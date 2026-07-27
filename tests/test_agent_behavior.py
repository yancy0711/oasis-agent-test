"""Behavioral tests that distinguish the agent from the fixed pipeline baseline."""
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point, box

from agent.memory import Context
from agent.orchestrator import Orchestrator
from agent.schemas import (ParetoPoint, PlanStep, Provenance, SiteSolution, TaskSpec,
                           ToolError, ToolResult, ValueWeights)
from tools.registry import registry
from tools.workflow import TOOL_CHAIN


class RecordingLLM:
    """Return canned structured responses and retain prompts for evidence checks."""

    def __init__(self, responses):
        """Store an ordered response queue and an empty call log."""
        self.responses, self.calls = list(responses), []

    def structured(self, system, user):
        """Record one prompt pair and return the next structured response."""
        self.calls.append({"system": system, "user": user})
        return self.responses.pop(0)


def test_planning_branches_differ_from_the_fixed_pipeline():
    """Facility, supplied-candidate, and heat-only tasks must not share one plan."""
    full = [{"tool": name, "args": {}} for name in TOOL_CHAIN]
    csv = [{"tool": name, "args": {}} for name in
           ["resolve_study_area", "prepare_demand", "load_candidate_sites", "build_travel_matrix", "optimize_sites", "generate_outputs"]]
    heat = [{"tool": name, "args": {}} for name in
            ["resolve_study_area", "prepare_demand", "summarize_heat_exposure"]]
    llm = RecordingLLM([{"steps": full}, {"steps": csv}, {"steps": heat}])
    orch = Orchestrator(llm, {})
    plans = [orch.plan(TaskSpec(place_name="Phoenix, Arizona, USA")),
             orch.plan(TaskSpec(place_name="Phoenix, Arizona, USA", candidate_source="csv", candidate_file="sites.csv")),
             orch.plan(TaskSpec(place_name="Phoenix, Arizona, USA", analysis_goal="heat_review"))]
    names = [[step.tool for step in plan.steps] for plan in plans]
    assert names[0] == TOOL_CHAIN
    assert "load_candidate_sites" in names[1] and "fetch_candidate_sites" not in names[1]
    assert names[2] == ["resolve_study_area", "prepare_demand", "summarize_heat_exposure"]
    assert names[1] != TOOL_CHAIN and names[2] != TOOL_CHAIN


def test_successful_but_insufficient_candidates_triggers_self_correction():
    """A suspicious success must be observed, repaired, retried, and logged."""
    calls = []

    @registry.register("candidate_probe_behavior_test", "Return candidate counts for an observation test.",
                       {"amenities": {"type": "array", "items": {"type": "string"}}})
    def candidate_probe(ctx, amenities):
        """Return too few candidates once and enough after amenity expansion."""
        calls.append(amenities)
        count = 2 if len(calls) == 1 else 6
        return ToolResult(ok=True, tool="candidate_probe_behavior_test", summary={"candidates": count})

    llm = RecordingLLM([{"tool": "candidate_probe_behavior_test",
                         "args": {"amenities": ["library", "school", "community_centre"]},
                         "rationale": "Broaden the public-facility search."}])
    orch = Orchestrator(llm, {"max_self_correction_rounds": 1})
    orch.ctx.put("task", TaskSpec(place_name="Smallville, USA", n_facilities=4,
                                  weights=ValueWeights()))
    result = orch.execute(PlanStep(tool="candidate_probe_behavior_test", args={"amenities": ["library"]}))
    stages = [event.stage for event in orch.ctx.history]
    assert result.ok and len(calls) == 2 and calls[0] != calls[1]
    assert "observation" in stages and "repair" in stages and stages[-1] == "tool_result"
    assert orch.ctx.history[-1].data["summary"]["candidates"] == 6


def test_conflicting_full_coverage_requirement_stops_for_hitl():
    """A low-coverage solution must surface a conflict instead of being accepted."""
    @registry.register("coverage_probe_behavior_test", "Return a tract-coverage summary for a conflict test.")
    def coverage_probe(ctx):
        """Return a mathematically valid but policy-inadequate solution summary."""
        return ToolResult(ok=True, tool="coverage_probe_behavior_test", summary={"covered_tract_ratio": .55})

    orch = Orchestrator(RecordingLLM([]), {"max_self_correction_rounds": 2})
    orch.ctx.put("task", TaskSpec(place_name="Phoenix, Arizona, USA", n_facilities=2,
                                  coverage_target=1.0, weights=ValueWeights()))
    result = orch.execute(PlanStep(tool="coverage_probe_behavior_test"))
    assert not result.ok and result.error.kind == "constraint_conflict" and not result.error.retryable
    assert [event.stage for event in orch.ctx.history] == ["tool_call", "observation"]
    assert "increase the facility budget or relax" in result.error.hint

def test_low_coverage_without_a_hard_target_is_valid():
    """A fixed-budget result remains valid when the user did not require full coverage."""
    @registry.register("low_coverage_behavior_test", "Return a valid low-coverage result.")
    def coverage_probe(ctx):
        """Return the same low ratio without a hard policy target."""
        return ToolResult(ok=True, tool="low_coverage_behavior_test", summary={"covered_tract_ratio": .155})

    orch = Orchestrator(RecordingLLM([]), {"max_self_correction_rounds": 0})
    orch.ctx.put("task", TaskSpec(place_name="New York, New York, USA", n_facilities=15,
                                  weights=ValueWeights()))
    result = orch.execute(PlanStep(tool="low_coverage_behavior_test"))
    assert result.ok and not result.error



def test_changed_facility_budget_can_exit_a_hard_coverage_conflict():
    """A revised HITL value must be re-solved instead of reusing a written conflict."""
    ctx = Context(); ctx.put("config", {"optimization": {"solver_time_limit_s": 5}})
    population = np.array([10., 10., 10.])
    ctx.put("demand", {"population": population, "vulnerable_mask": np.array([True, False, False]),
                       "indicators": {"population_density": population}})
    ctx.put("candidates", gpd.GeoDataFrame({"cand_id": ["a", "b", "c"]},
                                            geometry=[Point(0, 0), Point(1, 0), Point(2, 0)], crs="EPSG:4326"))
    ctx.put("travel", {"matrix": np.array([[5., 99., 99.], [99., 5., 99.], [99., 99., 5.]])})
    optimize = registry.get("optimize_sites").fn
    failed = optimize(ctx=ctx, n_facilities=2, threshold_minutes=15, coverage_target=1.0)
    solved = optimize(ctx=ctx, n_facilities=3, threshold_minutes=15, coverage_target=1.0)
    assert not failed.ok and failed.error.kind == "constraint_conflict"
    assert solved.ok and solved.summary["covered_tract_ratio"] == 1.0


def test_terminal_errors_receive_a_bounded_llm_recovery_proposal():
    """The model may propose typed edits but cannot alter protected task fields."""
    llm = RecordingLLM([{"diagnosis": "Two sites cannot satisfy the confirmed target.",
                         "question": "Increase the facility budget to three?",
                         "suggested_changes": {"n_facilities": 3, "place_name": "Elsewhere"}}])
    orch = Orchestrator(llm, {})
    task = TaskSpec(place_name="Phoenix, Arizona, USA", n_facilities=2,
                    coverage_target=1.0, weights=ValueWeights())
    result = ToolResult(ok=False, tool="optimize_sites",
                        summary={"max_tract_coverage_at_threshold": 1.0},
                        error=ToolError(kind="constraint_conflict", message="Infeasible", retryable=False))
    proposal = orch.propose_recovery(task, result)
    assert proposal.suggested_changes == {"n_facilities": 3}
    assert [event.stage for event in orch.ctx.history] == ["hitl", "hitl"]
    evidence = json.loads(llm.calls[0]["user"])
    assert evidence["failed_tool"] == "optimize_sites" and "place_name" not in evidence["allowed_changes"]
def test_result_interpretation_receives_only_structured_evidence():
    """Narrative generation must receive solver, Pareto, quality, and provenance metadata."""
    solution = SiteSolution(profile_name="requested", selected_ids=["a", "b"], covered_population=1200,
                            covered_ratio=.8, vulnerable_covered_ratio=.7, mean_travel_minutes=9,
                            weights_used=ValueWeights(), solver_status="Optimal")
    frontier = [ParetoPoint(alpha=0, efficiency_metric=.82, equity_metric=.65, solution=solution),
                ParetoPoint(alpha=1, efficiency_metric=.75, equity_metric=.78, solution=solution)]
    llm = RecordingLLM([{"decision_summary": "The confirmed solution covers 80% of the represented population.",
                         "self_reflection": "Language isolation is outside the four supplied indicators."}])
    orch = Orchestrator(llm, {})
    task = TaskSpec(place_name="Phoenix, Arizona, USA", weights=ValueWeights())
    orch.ctx.put("analysis", {"solution": solution, "frontier": frontier})
    orch.ctx.put("quality", {"heat": {"missing_tracts": 2, "imputation": "median"}})
    orch.ctx.add_provenance(Provenance(source="US Census ACS", record_count=20))
    narrative = orch.interpret_results(task)
    evidence = json.loads(llm.calls[0]["user"])
    assert narrative.decision_summary and narrative.self_reflection
    assert evidence["solution"]["covered_ratio"] == .8 and len(evidence["frontier"]) == 2
    assert evidence["quality"]["heat"]["imputation"] == "median"
    assert evidence["provenance"][0]["source"] == "US Census ACS" and "geometry" not in llm.calls[0]["user"]
    assert orch.ctx.history[-1].stage == "explanation"


def test_csv_candidate_tool_uses_supplied_points(tmp_path):
    """The CSV branch must load points without invoking OpenStreetMap."""
    path = tmp_path / "sites.csv"
    pd.DataFrame({"cand_id": ["a", "b"], "lat": [1.0, 1.5], "lon": [1.0, 1.5]}).to_csv(path, index=False)
    orch = Orchestrator(RecordingLLM([]), {})
    boundary = gpd.GeoDataFrame(geometry=[box(0, 0, 2, 2)], crs="EPSG:4326")
    orch.ctx.put("study", {"boundary": boundary})
    result = registry.get("load_candidate_sites").fn(ctx=orch.ctx, candidate_file=str(path))
    assert result.ok and result.summary["candidates"] == 2
    assert orch.ctx.get("candidates")["cand_id"].tolist() == ["a", "b"]

def test_parser_normalizes_null_optional_fields_from_live_models():
    """Non-applicable nullable fields must use defaults before TaskSpec validation."""
    llm = RecordingLLM([{"place_name": "Phoenix, Arizona, USA", "analysis_goal": "heat_review",
                         "candidate_source": None, "preference_question": None,
                         "n_facilities": 8, "threshold_minutes": 15, "weights": None}])
    task = Orchestrator(llm, {}).parse_task("Only show the hottest Phoenix communities")
    assert task.analysis_goal == "heat_review" and task.candidate_source == "osm"
    assert task.preference_question == "" and task.is_complete()

def test_insufficient_csv_candidates_require_hitl_instead_of_query_repair(tmp_path):
    """A fixed user candidate list cannot be broadened like an OSM query."""
    path = tmp_path / "one_site.csv"
    pd.DataFrame({"cand_id": ["a"], "lat": [1.0], "lon": [1.0]}).to_csv(path, index=False)
    orch = Orchestrator(RecordingLLM([]), {"max_self_correction_rounds": 2})
    orch.ctx.put("study", {"boundary": gpd.GeoDataFrame(geometry=[box(0, 0, 2, 2)], crs="EPSG:4326")})
    orch.ctx.put("task", TaskSpec(place_name="Test City, USA", candidate_source="csv",
                                  candidate_file=str(path), n_facilities=3, weights=ValueWeights()))
    result = orch.execute(PlanStep(tool="load_candidate_sites", args={"candidate_file": str(path)}))
    assert not result.ok and result.error.kind == "constraint_conflict" and not result.error.retryable
    assert "Upload more candidate points" in result.error.hint
    assert "repair" not in [event.stage for event in orch.ctx.history]

def test_repository_phoenix_candidate_sample_is_loadable():
    """Verify the committed CSV sample satisfies the user-candidate tool contract."""
    path = Path("data/samples/phoenix_candidate_sites.csv")
    frame = pd.read_csv(path)
    assert len(frame) == 10 and {"cand_id", "name", "lat", "lon"}.issubset(frame.columns)
    orch = Orchestrator(RecordingLLM([]), {})
    orch.ctx.put("study", {"boundary": gpd.GeoDataFrame(
        geometry=[box(-112.20, 33.30, -111.90, 33.60)], crs="EPSG:4326")})
    result = registry.get("load_candidate_sites").fn(ctx=orch.ctx, candidate_file=str(path))
    assert result.ok and result.summary["candidates"] == 10


def test_heat_only_tool_writes_a_folium_map(tmp_path):
    """Verify heat review returns both ranked tracts and a persisted map path."""
    areas = gpd.GeoDataFrame({"GEOID": ["1", "2"], "heat_exposure": [39., 42.]},
                             geometry=[box(-112.10, 33.42, -112.05, 33.47),
                                       box(-112.05, 33.42, -112.00, 33.47)], crs="EPSG:4326")
    boundary = gpd.GeoDataFrame(geometry=[box(-112.11, 33.41, -111.99, 33.48)], crs="EPSG:4326")
    orch = Orchestrator(RecordingLLM([]), {"output": {"dir": str(tmp_path)}})
    orch.ctx.put("study", {"boundary": boundary, "cache_scope": "phoenix_test"})
    orch.ctx.put("demand", {"areas": areas})
    result = registry.get("summarize_heat_exposure").fn(ctx=orch.ctx, limit=2)
    assert result.ok and result.summary["top_tracts"][0]["GEOID"] == "2"
    assert Path(result.summary["map"]).is_file() and Path(result.summary["map"]).name == "heat_exposure_map.html"

def test_constraint_validator_catches_regional_minimum_before_data_tools():
    """A regional minimum above the global budget must fail without spatial acquisition."""
    from agent.schemas import RegionalRequirement
    ctx = Context(); ctx.put("task", TaskSpec(place_name="Phoenix, Arizona, USA", n_facilities=2,
                                               regional_requirements=[RegionalRequirement(region="north", minimum_facilities=3)]))
    result = registry.get("validate_constraints").fn(ctx=ctx)
    assert not result.ok and result.error.kind == "constraint_conflict"
    assert result.summary["conflicts"][0]["minimum_facilities"] == 3


def test_reachability_analysis_ranks_the_hardest_tract():
    """Matrix diagnostics must identify the largest nearest-candidate time without an LLM."""
    ctx = Context(); areas = gpd.GeoDataFrame({"GEOID": ["easy", "hard"]},
                                               geometry=[Point(0, 0), Point(1, 1)], crs="EPSG:4326")
    ctx.put("demand", {"areas": areas, "population": np.ones(2),
                       "vulnerable_mask": np.array([False, True])})
    ctx.put("travel", {"matrix": np.array([[4., 7.], [18., 25.]])})
    result = registry.get("analyze_reachability").fn(ctx=ctx, threshold_minutes=15,
                                                      coverage_group="all", limit=2)
    assert result.ok and result.summary["hardest_demand"][0]["demand_id"] == "hard"
    assert result.summary["unserved_demand_ids"] == ["hard"]


def test_action_loop_replans_after_a_terminal_observation():
    """A failed action must become evidence for a different next action instead of ending a fixed plan."""
    calls = []

    @registry.register("loop_conflict_behavior_test", "Return one terminal feasibility observation.")
    def conflict(ctx):
        """Return a non-retryable conflict for action-loop testing."""
        calls.append("conflict")
        return ToolResult(ok=False, tool="loop_conflict_behavior_test",
                          error=ToolError(kind="constraint_conflict", message="budget infeasible", retryable=False))

    @registry.register("loop_diagnostic_behavior_test", "Diagnose the prior feasibility observation.")
    def diagnose(ctx):
        """Store a completed reachability diagnosis after the conflict."""
        calls.append("diagnose"); ctx.put("reachability_analysis", {"minimum": 3})
        return ToolResult(ok=True, tool="loop_diagnostic_behavior_test", handle="reachability_analysis",
                          summary={"minimum": 3})

    llm = RecordingLLM([
        {"kind": "tool", "tool": "loop_conflict_behavior_test", "rationale": "Test feasibility."},
        {"kind": "tool", "tool": "loop_diagnostic_behavior_test", "rationale": "Diagnose the conflict."},
    ])
    task = TaskSpec(place_name="Test City, USA", analysis_goal="reachability_review")
    result = Orchestrator(llm, {"max_agent_steps": 4, "max_self_correction_rounds": 0}).run(task, explain=False)
    assert result["ok"] and calls == ["conflict", "diagnose"]
    assert [event.stage for event in result["history"]].count("decision") == 3
    assert json.loads(llm.calls[1]["user"])["observations"][-1]["error"]["kind"] == "constraint_conflict"

def test_action_loop_stops_repeated_successful_calls():
    """Repeated identical successes must stop safely before the action limit."""
    @registry.register("repeat_success_behavior_test", "Return success without completing the requested goal.")
    def succeed(ctx):
        """Return a successful observation without a terminal handle."""
        return ToolResult(ok=True, tool="repeat_success_behavior_test", summary={"ready": True})

    action = {"kind": "tool", "tool": "repeat_success_behavior_test", "rationale": "Repeat the same probe."}
    llm = RecordingLLM([action, action, action])
    result = Orchestrator(llm, {"max_agent_steps": 10}).run(
        TaskSpec(place_name="New York, New York, USA", weights=ValueWeights()), explain=False)
    assert not result["ok"] and len(llm.calls) == 3 and len(result["results"]) == 3
    assert result["results"][-1].error.message == "The same successful tool call was selected again."

def test_action_loop_stops_as_soon_as_the_goal_handle_exists():
    """A verified output handle must stop the framework without another LLM call."""
    @registry.register("terminal_handle_behavior_test", "Create the requested output handle.")
    def complete(ctx):
        """Store the terminal handle used by a facility-siting task."""
        ctx.put("outputs", {"report": "ready"})
        return ToolResult(ok=True, tool="terminal_handle_behavior_test", handle="outputs")

    llm = RecordingLLM([{"kind": "tool", "tool": "terminal_handle_behavior_test",
                         "rationale": "Create the requested outputs."}])
    result = Orchestrator(llm, {"max_agent_steps": 6}).run(
        TaskSpec(place_name="New York, New York, USA", weights=ValueWeights()), explain=False)
    assert result["ok"] and len(llm.calls) == 1 and len(result["results"]) == 1

def test_completed_analysis_generates_outputs_without_another_llm_call(monkeypatch):
    """A completed optimization must enter output generation without model routing."""
    @registry.register("analysis_ready_behavior_test", "Create a completed analysis handle.")
    def analyze(ctx):
        """Store the handle that marks optimization as complete."""
        solution = SiteSolution(profile_name="requested", selected_ids=["a"], covered_population=1,
                                covered_ratio=1, vulnerable_covered_ratio=1, mean_travel_minutes=1,
                                weights_used=ValueWeights(), solver_status="Optimal")
        ctx.put("analysis", {"solution": solution})
        return ToolResult(ok=True, tool="analysis_ready_behavior_test", handle="analysis")

    def generate(ctx, output_dir=None, indicator="heat_exposure"):
        """Store lightweight output paths for the orchestration regression test."""
        paths = {"map": "map.html", "pareto": "pareto.png", "report": "report.md"}
        ctx.put("outputs", paths)
        return ToolResult(ok=True, tool="generate_outputs", handle="outputs", summary=paths)

    monkeypatch.setattr(registry.get("generate_outputs"), "fn", generate)
    llm = RecordingLLM([{"kind": "tool", "tool": "analysis_ready_behavior_test",
                         "rationale": "Complete the optimization."}])
    result = Orchestrator(llm, {"max_agent_steps": 6}).run(
        TaskSpec(place_name="New York, New York, USA", weights=ValueWeights()), explain=False)
    assert result["ok"] and len(llm.calls) == 1
    assert [item.tool for item in result["results"]] == ["analysis_ready_behavior_test", "generate_outputs"]
    assert result["history"][-1].data["kind"] == "finish"

def test_parser_preserves_diagnostic_goal_group_and_regional_constraints():
    """Typed parsing must retain reusable diagnostic intent instead of flattening it into notes."""
    llm = RecordingLLM([{"place_name": "Phoenix, Arizona, USA", "analysis_goal": "minimum_facilities",
                         "coverage_target": 1.0, "coverage_group": "vulnerable",
                         "regional_requirements": [{"region": "north", "minimum_facilities": 3}],
                         "n_facilities": 2, "threshold_minutes": 15, "weights": None}])
    task = Orchestrator(llm, {}).parse_task("How many sites cover all vulnerable tracts?")
    assert task.analysis_goal == "minimum_facilities" and task.coverage_group == "vulnerable"
    assert task.regional_requirements[0].region == "north" and task.is_complete()


def test_action_binding_protects_confirmed_diagnostic_parameters():
    """A model-selected diagnostic tool must receive confirmed TaskSpec values."""
    llm = RecordingLLM([{"kind": "tool", "tool": "find_minimum_threshold",
                         "args": {"n_facilities": 99, "coverage_target": .2, "coverage_group": "all"},
                         "rationale": "Find the exact boundary."}])
    task = TaskSpec(place_name="Phoenix, Arizona, USA", analysis_goal="minimum_threshold",
                    n_facilities=2, coverage_target=1.0, coverage_group="vulnerable")
    action = Orchestrator(llm, {}).decide_next(task, [], 1)
    assert action.args == {"n_facilities": 2, "coverage_target": 1.0, "coverage_group": "vulnerable"}


def test_scenario_comparison_reuses_saved_solver_results():
    """Counterfactual comparison must compute deltas without a scenario-specific solver."""
    ctx = Context(); ctx.put("scenario_results", [
        {"n_facilities": 2, "threshold_minutes": 15., "candidate_count": 5, "selected_count": 2,
         "covered_population_ratio": .5, "vulnerable_covered_ratio": .4,
         "covered_tract_ratio": .5, "all_tract_ratio": .5},
        {"n_facilities": 2, "threshold_minutes": 20., "candidate_count": 5, "selected_count": 2,
         "covered_population_ratio": .8, "vulnerable_covered_ratio": .7,
         "covered_tract_ratio": .75, "all_tract_ratio": .75},
    ])
    result = registry.get("compare_scenarios").fn(ctx=ctx)
    assert result.ok and result.summary["delta"]["threshold_minutes"] == 5
    assert round(result.summary["delta"]["covered_population_ratio"], 6) == .3

def test_action_loop_handles_a_regional_budget_contradiction_before_spatial_work():
    """The selected validator must turn a generic count contradiction directly into HITL."""
    from agent.schemas import RegionalRequirement
    llm = RecordingLLM([
        {"kind": "tool", "tool": "validate_constraints", "rationale": "Check arithmetic constraints first."},
        {"kind": "hitl", "message": "Increase the total budget or reduce the northern minimum.",
         "suggested_changes": {"n_facilities": 3}},
    ])
    task = TaskSpec(place_name="Phoenix, Arizona, USA", n_facilities=2, weights=ValueWeights(),
                    regional_requirements=[RegionalRequirement(region="north", minimum_facilities=3)])
    result = Orchestrator(llm, {"max_agent_steps": 3, "max_self_correction_rounds": 0}).run(task, explain=False)
    assert not result["ok"] and result["paused"] and result["recovery"].suggested_changes == {"n_facilities": 3}
    assert [item.tool for item in result["results"]] == ["validate_constraints"]
    assert not any(handle in result["diagnostics"] for handle in ("minimum_facilities", "minimum_threshold"))

def test_heat_review_uses_its_dependency_chain_without_action_loop_calls(monkeypatch):
    """A confirmed heat-only goal must resolve, prepare, and summarize deterministically."""
    def resolve(ctx, place_name):
        """Store a lightweight resolved study area."""
        ctx.put("study", {"place_name": place_name})
        return ToolResult(ok=True, tool="resolve_study_area", handle="study")

    def prepare(ctx):
        """Store lightweight prepared heat demand."""
        ctx.put("demand", {"ready": True})
        return ToolResult(ok=True, tool="prepare_demand", handle="demand")

    def summarize(ctx, limit=10):
        """Store a lightweight heat summary for the orchestration regression test."""
        summary = {"top_tracts": [], "tracts_reviewed": 1, "map": "heat.html"}
        ctx.put("heat_summary", summary)
        return ToolResult(ok=True, tool="summarize_heat_exposure", handle="heat_summary", summary=summary)

    monkeypatch.setattr(registry.get("resolve_study_area"), "fn", resolve)
    monkeypatch.setattr(registry.get("prepare_demand"), "fn", prepare)
    monkeypatch.setattr(registry.get("summarize_heat_exposure"), "fn", summarize)
    llm = RecordingLLM([])
    result = Orchestrator(llm, {"max_agent_steps": 6}).run(
        TaskSpec(place_name="Phoenix, Arizona, USA", analysis_goal="heat_review"), explain=False)
    assert result["ok"] and not llm.calls
    assert [item.tool for item in result["results"]] == ["resolve_study_area", "prepare_demand", "summarize_heat_exposure"]

def test_hard_constraint_hitl_reports_exact_relaxation_bounds(monkeypatch):
    """A stopped hard-coverage run must report exact facility and time alternatives."""
    @registry.register("infeasible_analysis_behavior_test", "Return an infeasible hard-coverage result.")
    def fail(ctx):
        """Store spatial prerequisites and an infeasible analysis observation."""
        for handle in ("demand", "candidates", "travel"): ctx.put(handle, {"ready": True})
        solution = SiteSolution(profile_name="requested", selected_ids=[], covered_population=0,
                                covered_ratio=0, vulnerable_covered_ratio=0, mean_travel_minutes=0,
                                weights_used=ValueWeights(), solver_status="Infeasible")
        ctx.put("analysis", {"solution": solution})
        return ToolResult(ok=False, tool="infeasible_analysis_behavior_test",
                          error=ToolError(kind="constraint_conflict", message="The confirmed constraints are infeasible.",
                                          hint="Diagnose the binding budget or threshold.", retryable=False))

    def minimum_facilities(ctx, **kwargs):
        """Return the exact fixed-threshold lower bound."""
        summary = {"feasible": True, "minimum_facilities": 5, "threshold_minutes": 15}
        ctx.put("minimum_facilities", summary)
        return ToolResult(ok=True, tool="find_minimum_facilities", handle="minimum_facilities", summary=summary)

    def minimum_threshold(ctx, **kwargs):
        """Return the exact fixed-budget threshold lower bound."""
        summary = {"feasible": True, "minimum_threshold_minutes": 24.0}
        ctx.put("minimum_threshold", summary)
        return ToolResult(ok=True, tool="find_minimum_threshold", handle="minimum_threshold", summary=summary)

    monkeypatch.setattr(registry.get("find_minimum_facilities"), "fn", minimum_facilities)
    monkeypatch.setattr(registry.get("find_minimum_threshold"), "fn", minimum_threshold)
    llm = RecordingLLM([
        {"kind": "tool", "tool": "infeasible_analysis_behavior_test", "rationale": "Test the hard constraints."},
        {"kind": "hitl", "message": "Which confirmed constraint should change?"},
    ])
    task = TaskSpec(place_name="Phoenix, Arizona, USA", n_facilities=2, threshold_minutes=15,
                    coverage_target=1.0, weights=ValueWeights())
    result = Orchestrator(llm, {"max_agent_steps": 5, "max_self_correction_rounds": 0}).run(task, explain=False)
    assert not result["ok"] and result["paused"] and result["outputs"] is None
    assert "at least 5 facilities" in result["recovery"].diagnosis
    assert "at least 24.0 minutes" in result["recovery"].diagnosis
    assert result["recovery"].suggested_changes == {"n_facilities": 5}
    assert [item.tool for item in result["results"]] == ["infeasible_analysis_behavior_test",
                                                          "find_minimum_facilities", "find_minimum_threshold"]

def test_hard_coverage_with_travel_requires_feasibility_optimization():
    """A hard target with travel evidence must be tested before the model may stop."""
    orch = Orchestrator(RecordingLLM([]), {})
    orch.ctx.put("travel", {"matrix": [[1.0]]})
    task = TaskSpec(place_name="Phoenix, Arizona, USA", n_facilities=2,
                    threshold_minutes=15, coverage_target=1.0, weights=ValueWeights())
    assert orch._completion_tool(task) == "optimize_sites"

def test_fixed_count_full_coverage_is_not_misparsed_as_a_minimum_query():
    """An explicit one-site budget must remain siting even when full coverage is required."""
    llm = RecordingLLM([{"place_name": "San Francisco, California, USA",
                         "analysis_goal": "minimum_facilities", "n_facilities": 1,
                         "threshold_minutes": 15, "coverage_target": 1.0, "weights": None}])
    task = Orchestrator(llm, {}).parse_task(
        "Give me a plan of setting 1 site in San Francisco, and every tract must be reachable within 15 minutes.")
    assert task.analysis_goal == "facility_siting" and task.n_facilities == 1 and task.coverage_target == 1.0


def test_explicit_minimum_facility_question_keeps_diagnostic_goal():
    """An explicit how-many question must remain an LSCP diagnostic request."""
    llm = RecordingLLM([{"place_name": "San Francisco, California, USA",
                         "analysis_goal": "minimum_facilities", "n_facilities": 1,
                         "threshold_minutes": 15, "coverage_target": 1.0, "weights": None}])
    task = Orchestrator(llm, {}).parse_task("How many sites are needed to cover every tract within 15 minutes?")
    assert task.analysis_goal == "minimum_facilities"

def test_explicit_minute_threshold_overrides_a_missing_llm_field():
    """An explicit minute value is factual input and must not fall back to configuration."""
    llm = RecordingLLM([{"place_name": "Washington, District of Columbia, USA",
                         "analysis_goal": "minimum_facilities", "n_facilities": 8,
                         "threshold_minutes": None, "coverage_target": 1.0, "weights": None}])
    task = Orchestrator(llm, {"accessibility": {"threshold_minutes": 15}}).parse_task(
        "How many sites allow every tract to be reachable within 35 minutes in Washington, District of Columbia, USA?")
    assert task.analysis_goal == "minimum_facilities" and task.threshold_minutes == 35
    assert "threshold_minutes" not in task.notes
