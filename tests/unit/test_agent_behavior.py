"""Behavioral tests that distinguish the agent from the fixed pipeline baseline."""
import json
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Point, box

from oasis.agent.context import Context
from oasis.agent.orchestrator import LLMClient, MissingPlaceError, Orchestrator
from oasis.agent.scope import classify_request, route_origin_from_prompt
from oasis.contracts.domain import (ParetoPoint, PlanStep, Provenance, SiteSolution, TaskSpec,
                           ToolError, ToolResult, ValueWeights)
from oasis.services.registry import registry
from oasis.services.workflow import TOOL_CHAIN


class RecordingLLM:
    """Return canned structured responses and retain prompts for evidence checks."""

    def __init__(self, responses):
        """Store an ordered response queue and an empty call log."""
        self.responses, self.calls = list(responses), []

    def structured(self, system, user, schema=None):
        """Record one prompt pair and return the next structured response."""
        self.calls.append({"system": system, "user": user, "schema": schema})
        return self.responses.pop(0)


def test_requested_coverage_timeout_does_not_claim_a_constraint_conflict(monkeypatch):
    from oasis.services import workflow
    ctx = Context()
    ctx.put("config", {"optimization": {"solver_time_limit_s": 3}})
    ctx.put("demand", {"population": np.ones(2), "indicators": {}})
    ctx.put("candidates", gpd.GeoDataFrame({"cand_id": ["a", "b"]},
        geometry=[Point(0, 0), Point(1, 1)], crs="EPSG:4326"))
    ctx.put("travel", {"matrix": np.ones((2, 2))})
    monkeypatch.setattr(workflow, "coverage_pareto_sweep", lambda *args, **kwargs: [])
    def timed_out(*args, **kwargs):
        assert kwargs["cancel_check"] == ctx.check_cancelled
        kwargs["heartbeat"]()
        return SimpleNamespace(solver_status="Not Solved")
    monkeypatch.setattr(workflow, "solve_mclp", timed_out)
    result = workflow.optimize_sites(ctx, n_facilities=1, coverage_target=.8, weights=ValueWeights())
    assert not result.ok and result.error.kind == "timeout"
    assert "Feasibility has not been established" in result.error.hint
    orch = Orchestrator(RecordingLLM([]), {})
    orch.ctx = ctx
    steps = []
    orch._complete_constraint_diagnostics(TaskSpec(place_name="Riverside", coverage_target=.8), [result], steps)
    assert steps == [] and not ctx.has("minimum_facilities")
    recovery = orch._failure_recovery(
        TaskSpec(place_name="Riverside", n_facilities=10, threshold_minutes=10), [result],
        question="Adjust the number of facilities or the threshold minutes?",
    )
    assert recovery.suggested_changes == {}
    assert "Retry the same task" in recovery.question
    assert "does not prove infeasibility" in recovery.question


@pytest.mark.parametrize("expression", ["10 minutes' dirve", "a ten-minute car ride", "10 min by taxi"])
def test_semantic_driving_is_retained_with_equity_preference(expression):
    from oasis.settings import load_profiles
    llm = RecordingLLM([{
        "place_name": "Los Angeles, California, USA", "mobility_scenario": "driving",
        "threshold_minutes": 10, "recommended_value_profile": "equity_first", "weights": None,
    }])
    task = Orchestrator(llm, {"value_profiles": load_profiles()}).parse_task(
        f"I'm in LA. Plan cooling centers within {expression}, equity-first.")
    assert task.mobility_scenario == "driving" and task.threshold_minutes == 10
    assert task.recommended_value_profile == "equity_first" and task.weights is None
    assert "obvious typos" in llm.calls[0]["system"]


def test_portfolio_selection_near_landmark_does_not_become_point_navigation():
    task = Orchestrator(RecordingLLM([{
        "place_name": "Riverside, California, USA", "n_facilities": 10,
        "analysis_goal": "facility_siting", "coverage_target": .8,
        "route_origin": {"entity": "Raincross Hotel Riverside", "context": "Riverside", "source": "llm"},
    }]), {}).parse_task("Select 10 candidate cooling facilities near Raincross Hotel Riverside within a 15-minute walking threshold, covering 80% of the population.")
    assert task.route_origin is None and task.analysis_goal == "facility_siting"
    assert task.n_facilities == 10 and task.threshold_minutes == 15
    assert task.coverage_target == .8


def test_request_scope_routes_clear_boundaries_with_a_useful_alternative():
    unsupported = classify_request("Find vacant land for a new cooling-center building in Phoenix")
    assert unsupported.disposition == "out_of_scope"
    assert "parcel and zoning GIS" in unsupported.alternative
    assert classify_request("Phoenix").disposition == "needs_clarification"
    assert classify_request("Compare candidate facilities in Phoenix").disposition == "full_analysis"
    vague = classify_request("Help me improve cooling access.")
    assert vague.disposition == "needs_clarification"
    assert "US city or county" in vague.alternative
    realtime = classify_request("Which NYC cooling centers are open right now?")
    assert realtime.disposition == "out_of_scope"
    assert "311" in realtime.alternative
    assert classify_request("Select sites without performing zoning analysis in Phoenix").disposition == "full_analysis"
    assert classify_request("Select sites away from bus hubs in Phoenix").disposition == "full_analysis"


def test_parser_preserves_general_llm_result_query_intent():
    """The model, not an expanding keyword regex, selects the bounded ranking metric."""
    llm = RecordingLLM([{
        "place_name": "Washington, District of Columbia, USA",
        "analysis_goal": "tract_review",
        "place_names": ["Washington, District of Columbia, USA"],
        "result_queries": [{
            "metric": "combined_disadvantage", "direction": "highest", "limit": 3,
            "raw_text": "Which three tracts need attention considering vulnerability and access?",
        }],
    }])
    task = Orchestrator(llm, {}).parse_task(
        "Which three places in DC should I consider most when equity and accessibility are considered?"
    )
    assert task.analysis_goal == "tract_review"
    assert task.result_queries[0].metric == "combined_disadvantage"
    assert task.result_queries[0].limit == 3
    assert llm.calls[0]["schema"]["title"] == "TaskSpec"


def test_schema_first_parser_preserves_worded_top_n_from_the_model():
    """The typed model output keeps answer cardinality separate from the decision budget."""
    llm = RecordingLLM([{
        "place_name": "Washington, District of Columbia, USA",
        "place_names": ["Washington, District of Columbia, USA"],
        "analysis_goal": "tract_review",
        "result_queries": [{
            "metric": "combined_disadvantage", "direction": "highest", "limit": 5,
            "raw_text": "Which five places need attention?",
        }],
    }])

    task = Orchestrator(llm, {}).parse_task(
        "Which five places in DC should receive the most attention considering both vulnerability and accessibility?"
    )

    assert task.result_queries[0].limit == 5
    assert task.n_facilities == 10


def test_parser_retains_siting_and_ranking_as_two_outputs():
    llm = RecordingLLM([{
        "place_name": "Phoenix, Arizona, USA",
        "place_names": ["Phoenix, Arizona, USA"],
        "analysis_goal": "facility_siting",
        "n_facilities": 10,
        "result_queries": [{
            "metric": "combined_disadvantage", "direction": "highest", "limit": 1,
            "raw_text": "identify the most vulnerable tract with poor access",
        }],
    }])

    task = Orchestrator(llm, {}).parse_task(
        "Select 10 candidate facilities in Phoenix and identify the most vulnerable tract with poor access."
    )

    assert task.analysis_goal == "facility_siting"
    assert task.n_facilities == 10
    assert len(task.result_queries) == 1


def test_parser_preserves_multiple_place_names_for_human_confirmation():
    llm = RecordingLLM([{
        "place_name": "Phoenix, Arizona, USA",
        "place_names": ["Phoenix, Arizona, USA", "New York, New York, USA"],
        "ambiguities": [{
            "field": "place_names",
            "candidates": ["Phoenix, Arizona, USA", "New York, New York, USA"],
            "question": "Which study area should this run use?",
        }],
        "analysis_goal": "facility_siting",
        "n_facilities": 10,
    }])

    task = Orchestrator(llm, {}).parse_task(
        "Select 10 candidate facilities in Phoenix and NYC."
    )

    assert task.place_names == ["Phoenix, Arizona, USA", "New York, New York, USA"]
    assert task.place_name == "Phoenix, Arizona, USA"


@pytest.mark.parametrize(
    ("ranking", "expected_metric", "expected_direction"),
    [
        ("vulnerability_access_gap", "combined_disadvantage", "highest"),
        ({"metric": "vulnerability", "direction": "greatest"}, "vulnerability", "highest"),
        ({"metric": "heat", "direction": "bottom"}, "heat_exposure", "lowest"),
        ({"metric": "crime_rate", "direction": "highest"}, "unsupported", "highest"),
    ],
)
def test_parser_normalizes_provider_ranking_variants(ranking, expected_metric, expected_direction):
    """Provider-specific JSON variants are adapted before strict TaskSpec validation."""
    llm = RecordingLLM([{
        "place_name": "Phoenix, Arizona, USA",
        "analysis_goal": "tract_review",
        "tract_ranking": ranking,
    }])

    task = Orchestrator(llm, {}).parse_task("Rank the requested tract property in Phoenix.")

    assert task.result_queries[0].metric == expected_metric
    assert task.result_queries[0].direction == expected_direction


def test_parser_converges_optional_provider_variants_before_strict_validation():
    llm = RecordingLLM([{
        "place_name": "Phoenix, Arizona, USA",
        "analysis_goal": "equity_analysis",
        "intervention": "cooling_centre",
        "candidate_source": "openstreetmap",
        "mobility_scenario": "on foot",
        "result_queries": [{
            "metric": "vulnerability", "direction": "desc", "limit": 100,
            "scope": "vulnerable_only",
        }],
        "ambiguities": [{"field": "provider_private_guess", "question": "Ignore me."}],
    }])

    task = Orchestrator(llm, {}).parse_task("Review vulnerable tracts in Phoenix.")

    assert task.analysis_goal == "tract_review"
    assert task.intervention == "cooling_center"
    assert task.candidate_source == "osm"
    assert task.mobility_scenario == "walking"
    assert task.result_queries[0].direction == "highest"
    assert task.result_queries[0].limit == 50
    assert task.result_queries[0].scope == "vulnerable"
    assert task.ambiguities == []


def test_result_query_tool_returns_a_direct_auditable_answer(tmp_path):
    """A vulnerability question must produce ranked tract rows and a reusable map."""
    areas = gpd.GeoDataFrame({
        "GEOID": ["low", "high"],
        "heat_exposure": [39.0, 41.0],
        "vulnerability_score": [.2, .9],
    }, geometry=[Point(-112.1, 33.4), Point(-112.0, 33.5)], crs="EPSG:4326")
    ctx = Context()
    ctx.put("config", {"output": {"dir": str(tmp_path)}})
    ctx.put("study", {"boundary": areas})
    ctx.put("demand", {"areas": areas, "population": np.array([100., 100.])})
    ctx.put("task", TaskSpec(
        place_name="Test City", analysis_goal="tract_review",
        result_queries=[
            {"metric": "vulnerability", "limit": 1},
            {"metric": "heat_exposure", "limit": 2},
        ],
    ))
    result = registry.get("answer_result_queries").fn(ctx=ctx)
    assert result.ok
    answer = result.summary["answers"][0]
    assert answer["top_rows"][0]["GEOID"] == "high"
    assert "percentile-normalized" in answer["computation"]["data_basis"]
    assert answer["computation"]["formula"].startswith("V_i =")
    assert "threshold_minutes" not in answer["computation"]["parameters"]
    assert Path(answer["map"]).is_file()
    heat_answer = result.summary["answers"][1]
    assert len(heat_answer["top_rows"]) == 2
    assert "weights" not in heat_answer["computation"]["parameters"]


def test_route_request_extracts_named_origin_without_treating_it_as_study_area():
    prompt = "I'm in Lincoln Memorial, how can I find the nearest cooling site?"
    assert route_origin_from_prompt(prompt) == "Lincoln Memorial"
    llm = RecordingLLM([{
        "place_name": "Lincoln Memorial", "analysis_goal": "reachability_review",
        "n_facilities": 7, "threshold_minutes": 15, "weights": None,
    }])
    task = Orchestrator(llm, {"optimization": {"n_facilities": 10}}).parse_task(prompt)
    assert task.analysis_goal == "facility_siting"
    assert task.route_origin is not None
    assert task.route_origin.entity == "Lincoln Memorial"
    assert task.route_origin.source == "fallback"
    assert task.n_facilities == 10
    assert set(task.candidate_amenities) == {
        "library", "community_centre", "school", "sports_centre",
        "arts_centre", "place_of_worship",
    }


def test_llm_route_origin_structure_is_preserved_for_deterministic_search():
    prompt = "I'm at the eastern entrance of Yellowstone in Wyoming. Find the nearest cooling facility."
    llm = RecordingLLM([{
        "place_name": "",
        "analysis_goal": "facility_siting",
        "route_origin": {
            "entity": "Yellowstone National Park",
            "context": "Wyoming",
            "positional_hint": "eastern entrance",
            "raw_text": "the eastern entrance of Yellowstone in Wyoming",
            "source": "llm",
        },
    }])
    with pytest.raises(MissingPlaceError) as error:
        Orchestrator(llm, {"optimization": {"n_facilities": 10}}).parse_task(prompt)
    origin = error.value.task_data["route_origin"]
    assert origin["entity"] == "Yellowstone National Park"
    assert origin["context"] == "Wyoming"
    assert origin["positional_hint"] == "eastern entrance"
    assert origin["source"] == "llm"


def test_generic_park_gate_requires_a_named_park_before_geocoding():
    decision = classify_request("I'm at national park's gate. How can I find the nearest cooling facility?")
    assert decision.disposition == "needs_clarification"
    assert "Name the park" in decision.alternative
    named = classify_request("I'm at Yellowstone National Park's north entrance. How can I find the nearest cooling facility?")
    assert named.disposition == "full_analysis"


def test_open_ended_normative_build_question_is_out_of_scope():
    decision = classify_request("Should we build more cooling centers in Phoenix?")
    assert decision.disposition == "out_of_scope"
    assert "measurable diagnostic" in decision.alternative


def test_json_payload_accepts_reasoning_wrappers_and_rejects_plain_text_cleanly():
    wrapped = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content='<think>checking fields</think>\n```json\n{"analysis_goal":"heat_review"}\n```'
    ))])
    assert LLMClient._json_payload(wrapped) == {"analysis_goal": "heat_review"}
    plain = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="I cannot format that."))])
    try:
        LLMClient._json_payload(plain)
    except ValueError as exc:
        assert "structured JSON output" in str(exc)
    else:
        raise AssertionError("Plain text must not be accepted as structured JSON.")


def test_structured_output_retries_without_native_json_mode():
    """A provider that ignores JSON mode can recover through forced function calling."""
    calls = []
    responses = [
        SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=""), finish_reason="stop"
        )]),
        SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=None, tool_calls=[SimpleNamespace(
                function=SimpleNamespace(arguments='{"payload":"{\\"analysis_goal\\":\\"heat_review\\"}"}')
            )]),
            finish_reason="stop",
        )]),
    ]

    def create(**kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    client = LLMClient.__new__(LLMClient)
    client.cfg = {"provider": "openrouter", "model": "vendor/model"}
    client.client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)
    ))

    result = client.structured("Parse the request.", "Find the hottest Phoenix tracts.")

    assert result == {"analysis_goal": "heat_review"}
    assert len(calls) == 2
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in calls[1]
    assert calls[1]["tool_choice"]["function"]["name"] == "submit_json"
    assert calls[0]["extra_body"]["provider"]["require_parameters"] is False


def test_structured_output_uses_json_schema_when_one_is_supplied():
    calls = []
    response = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content='{"value":5}'), finish_reason="stop",
    )])
    client = LLMClient.__new__(LLMClient)
    client.cfg = {"provider": "openrouter", "model": "vendor/model"}
    client.client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=lambda **kwargs: calls.append(kwargs) or response)
    ))
    schema = {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}

    result = client.structured("Parse.", "Return five.", schema=schema)

    assert result == {"value": 5}
    assert calls[0]["response_format"]["type"] == "json_schema"
    assert calls[0]["response_format"]["json_schema"]["schema"] == schema
    assert calls[0]["extra_body"]["provider"]["require_parameters"] is True


def test_gemini_region_failure_stops_before_json_compatibility_retries():
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        raise RuntimeError(
            "Error code: 400 - User location is not supported for the API use. FAILED_PRECONDITION"
        )

    client = LLMClient.__new__(LLMClient)
    client.cfg = {"provider": "gemini", "model": "gemini-3.5-flash-lite"}
    client.client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)
    ))

    with pytest.raises(RuntimeError, match="provider-region restriction"):
        client.structured("Parse.", "Review Phoenix.", schema={"title": "TaskSpec", "type": "object"})
    assert len(calls) == 1


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

def test_parser_preserves_explicit_integer_facility_count_from_float_model_output():
    """An explicit user count wins even when a live model serializes it as 11.0."""
    llm = RecordingLLM([{"place_name": "Phoenix, Arizona, USA", "analysis_goal": "facility_siting",
                         "n_facilities": 11.0, "threshold_minutes": 10.0, "weights": None}])
    task = Orchestrator(llm, {"optimization": {"n_facilities": 8}}).parse_task(
        "Site 11 cooling centers in Phoenix, Arizona within 10 minutes."
    )
    assert task.n_facilities == 11
    assert "runtime defaults: n_facilities" not in task.notes


def test_parser_deterministically_preserves_select_facility_count():
    llm = RecordingLLM([{
        "place_name": "New York, New York, USA", "analysis_goal": "facility_siting",
        "n_facilities": 8, "threshold_minutes": 15, "weights": None,
    }])
    task = Orchestrator(llm, {"optimization": {"n_facilities": 8}}).parse_task(
        "Select 16 cooling facilities in NYC within a 15-minute walking threshold."
    )
    assert task.n_facilities == 16
    assert task.threshold_minutes == 15


@pytest.mark.parametrize(
    ("prompt", "payload", "field", "expected"),
    [
        (
            "Site 200 cooling centers in Los Angeles.",
            {"place_name": "Los Angeles, California, USA", "n_facilities": 200},
            "n_facilities", 10,
        ),
        (
            "Select 0 facilities in Phoenix.",
            {"place_name": "Phoenix, Arizona, USA", "n_facilities": 0},
            "n_facilities", 10,
        ),
        (
            "Select cooling facilities in Phoenix within 0 minutes.",
            {"place_name": "Phoenix, Arizona, USA", "threshold_minutes": 0},
            "threshold_minutes", 15,
        ),
    ],
)
def test_explicit_out_of_range_numbers_fall_back_to_typed_hitl(
    prompt, payload, field, expected,
):
    task = Orchestrator(
        RecordingLLM([payload]),
        {"optimization": {"n_facilities": 10},
         "accessibility": {"threshold_minutes": 15}},
    ).parse_task(prompt)

    assert getattr(task, field) == expected
    assert any(item.field == field for item in task.ambiguities)
    assert "retained" in task.notes


def test_sensitivity_lists_abstain_from_scalar_regex_overrides():
    llm = RecordingLLM([{
        "place_name": "Phoenix, Arizona, USA",
        "analysis_goal": "sensitivity_analysis",
        "n_facilities": 10,
        "threshold_minutes": 15,
        "sensitivity_facility_counts": [5, 10, 15],
        "sensitivity_thresholds": [10, 15, 20],
    }])
    task = Orchestrator(llm, {}).parse_task(
        "Test 5, 10 and 15 facilities at 10, 15 and 20 minutes in Phoenix."
    )

    assert task.n_facilities == 10 and task.threshold_minutes == 15
    assert task.sensitivity_facility_counts == [5, 10, 15]
    assert task.sensitivity_thresholds == [10, 15, 20]
    assert not any(item.field in {"n_facilities", "threshold_minutes"}
                   for item in task.ambiguities)


def test_parser_normalizes_model_notes_list_to_text():
    llm = RecordingLLM([{
        "place_name": "Phoenix, Arizona, USA", "analysis_goal": "facility_siting",
        "n_facilities": 16, "threshold_minutes": 15, "weights": None,
        "notes": [],
    }])
    task = Orchestrator(llm, {}).parse_task(
        "Select 16 candidate cooling facilities in Phoenix within 15 minutes."
    )
    assert task.notes == ""

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


def test_heat_only_tool_writes_maplibre_data(tmp_path):
    """Verify heat review returns both ranked tracts and a persisted map path."""
    areas = gpd.GeoDataFrame({"GEOID": ["1", "2"], "heat_exposure": [39., 42.]},
                             geometry=[box(-112.10, 33.42, -112.05, 33.47),
                                       box(-112.05, 33.42, -112.00, 33.47)], crs="EPSG:4326")
    boundary = gpd.GeoDataFrame(geometry=[box(-112.11, 33.41, -111.99, 33.48)], crs="EPSG:4326")
    orch = Orchestrator(RecordingLLM([]), {"output": {"dir": str(tmp_path)}})
    orch.ctx.put("study", {"boundary": boundary, "cache_scope": "phoenix_test"})
    orch.ctx.put("task", TaskSpec(place_name="Phoenix, Arizona, USA", analysis_goal="heat_review"))
    orch.ctx.put("demand", {"areas": areas})
    result = registry.get("summarize_heat_exposure").fn(ctx=orch.ctx, limit=2)
    assert result.ok and result.summary["top_tracts"][0]["GEOID"] == "2"
    assert Path(result.summary["map"]).is_file() and Path(result.summary["map"]).name == "heat_exposure_map.geojson"
    assert Path(result.summary["analysis_chart"]).is_file() and Path(result.summary["report"]).is_file()
    assert result.summary["statistics"]["morans_i"] is not None

def test_constraint_validator_catches_regional_minimum_before_data_tools():
    """A regional minimum above the global budget must fail without spatial acquisition."""
    from oasis.contracts.domain import RegionalRequirement
    ctx = Context(); ctx.put("task", TaskSpec(place_name="Phoenix, Arizona, USA", n_facilities=2,
                                               regional_requirements=[RegionalRequirement(region="north", minimum_facilities=3)]))
    result = registry.get("validate_constraints").fn(ctx=ctx)
    assert not result.ok and result.error.kind == "constraint_conflict"
    assert result.summary["conflicts"][0]["minimum_facilities"] == 3


def test_reachability_analysis_ranks_the_hardest_tract(tmp_path):
    """Matrix diagnostics must identify the largest nearest-candidate time without an LLM."""
    ctx = Context(); ctx.put("config", {"output": {"dir": str(tmp_path)}})
    areas = gpd.GeoDataFrame({"GEOID": ["easy", "hard"]},
                                               geometry=[Point(0, 0), Point(1, 1)], crs="EPSG:4326")
    ctx.put("study", {"boundary": areas[["geometry"]].copy()})
    ctx.put("demand", {"areas": areas, "population": np.ones(2),
                       "vulnerable_mask": np.array([False, True])})
    ctx.put("travel", {"matrix": np.array([[4., 7.], [18., 25.]])})
    result = registry.get("analyze_reachability").fn(ctx=ctx, threshold_minutes=15,
                                                      coverage_group="all", limit=2)
    assert result.ok and result.summary["hardest_demand"][0]["demand_id"] == "hard"
    assert result.summary["unserved_demand_ids"] == ["hard"]
    assert Path(result.summary["map"]).is_file()


def test_completed_reachability_review_returns_the_requested_answer():
    """A successful quick diagnostic must name the hardest tract, not only say completed."""
    orch = Orchestrator(RecordingLLM([]), {"max_agent_steps": 1})
    orch.ctx.put("reachability_analysis", {
        "threshold_minutes": 15,
        "unserved_demand_ids": ["hard"],
        "hardest_demand": [{"demand_id": "hard", "nearest_minutes": 18.0,
                            "reachable_candidates": 0}],
    })
    result = orch.run(
        TaskSpec(place_name="Test City, USA", analysis_goal="reachability_review"),
        explain=False,
    )
    assert result["ok"]
    assert "Tract hard" in result["message"] and "18.0 minutes" in result["message"]


def test_completed_heat_review_returns_the_hottest_tract():
    """A successful heat diagnostic must name the highest-exposure tract and value."""
    orch = Orchestrator(RecordingLLM([]), {"max_agent_steps": 1})
    orch.ctx.put("heat_summary", {
        "tracts_reviewed": 2,
        "top_tracts": [{"GEOID": "hot", "heat_exposure": 42.5}],
    })
    result = orch.run(
        TaskSpec(place_name="Test City, USA", analysis_goal="heat_review"),
        explain=False,
    )
    assert result["ok"]
    assert "Tract hot" in result["message"] and "42.5 °C" in result["message"]
    assert "Basis:" in result["message"]


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
    assert [event.stage for event in result["history"]].count("decision") >= 3
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


def test_parser_separates_driving_mode_from_named_value_profiles():
    llm = RecordingLLM([{
        "place_name": "Washington, District of Columbia, USA",
        "analysis_goal": "scenario_comparison", "mobility_scenario": "walking",
        "n_facilities": 2, "threshold_minutes": 15, "weights": None,
    }])
    profiles = {
        "equity_first": {"label": "Equity first", "weights": ValueWeights().model_dump()},
        "balanced": {"label": "Balanced", "weights": ValueWeights().model_dump()},
    }
    task = Orchestrator(llm, {"value_profiles": profiles}).parse_task(
        "Apply the driving-first and equity-first profile and compare it with the balanced profile in DC."
    )
    assert task.mobility_scenario == "driving"
    assert task.comparison_profiles == ["equity_first", "balanced"]


def test_explicit_bike_mode_overrides_model_default_without_clarification():
    llm = RecordingLLM([{
        "place_name": "Las Vegas, Nevada, USA",
        "analysis_goal": "facility_siting",
        "n_facilities": 10,
        "mobility_scenario": "walking",
        "ambiguities": [{
            "field": "mobility_scenario", "candidates": ["walking", "cycling"],
            "question": "Should this run use walking or cycling travel?",
        }],
    }])
    task = Orchestrator(llm, {}).parse_task(
        "Pick 10 sites in Las Vegas, Nevada. People will ride a BIKE."
    )
    assert task.mobility_scenario == "cycling"
    assert not any(item.field == "mobility_scenario" for item in task.ambiguities)


def test_parser_suggests_registered_efficiency_and_equity_profiles_for_generic_comparison():
    llm = RecordingLLM([{
        "place_name": "Phoenix, Arizona, USA", "analysis_goal": "scenario_comparison",
        "mobility_scenario": "walking", "n_facilities": 10, "threshold_minutes": 15,
        "weights": None,
    }])
    profiles = {
        "efficiency_first": {"label": "Efficiency first", "weights": ValueWeights().model_dump()},
        "equity_first": {"label": "Equity first", "weights": ValueWeights().model_dump()},
        "balanced": {"label": "Balanced", "weights": ValueWeights().model_dump()},
    }
    task = Orchestrator(llm, {"value_profiles": profiles}).parse_task(
        "Compare efficiency and access inequality when selecting 10 candidate cooling facilities in Phoenix within a 15-minute walk."
    )
    assert task.comparison_profiles == ["efficiency_first", "equity_first"]


def test_clear_efficiency_inequality_comparison_overrides_a_misread_tract_query():
    prompt = (
        "Compare efficiency and access inequality when selecting 10 candidate cooling "
        "facilities in Phoenix within a 15-minute walk."
    )
    llm = RecordingLLM([{
        "place_name": "Phoenix, Arizona, USA", "analysis_goal": "tract_review",
        "n_facilities": 10, "threshold_minutes": 15,
        "result_queries": [{
            "metric": "access_disadvantage", "direction": "highest", "limit": 10,
            "raw_text": prompt,
        }],
    }])
    profiles = {
        "efficiency_first": {"label": "Efficiency first", "weights": ValueWeights().model_dump()},
        "equity_first": {"label": "Equity first", "weights": ValueWeights().model_dump()},
    }
    task = Orchestrator(llm, {"value_profiles": profiles}).parse_task(prompt)
    assert task.analysis_goal == "scenario_comparison"
    assert task.comparison_profiles == ["efficiency_first", "equity_first"]
    assert task.result_queries == []


def test_tool_name_in_action_kind_is_normalized_to_a_tool_action():
    llm = RecordingLLM([{"kind": "compare_scenarios", "rationale": "Compare saved results."}])
    action = Orchestrator(llm, {}).decide_next(
        TaskSpec(place_name="Washington, District of Columbia, USA",
                 analysis_goal="scenario_comparison"), [], 1
    )
    assert action.kind == "tool" and action.tool == "compare_scenarios"


def test_value_profile_comparison_uses_one_shared_travel_baseline():
    ctx = Context()
    profiles = {
        "efficiency_first": {"label": "Efficiency first", "weights": {
            "population_density": 1, "elderly_ratio": 0, "poverty_rate": 0,
            "heat_exposure": 0, "environment_burden": 0}},
        "equity_first": {"label": "Equity first", "weights": {
            "population_density": .1, "elderly_ratio": .3, "poverty_rate": .3,
            "heat_exposure": .3, "environment_burden": 0}},
    }
    ctx.put("config", {"value_profiles": profiles, "optimization": {"solver_time_limit_s": 10}})
    ctx.put("task", TaskSpec(place_name="Test City, USA", analysis_goal="scenario_comparison",
                              comparison_profiles=list(profiles)))
    ctx.put("demand", {"population": np.array([100., 100.]),
                       "equity_population": np.array([0., 100.]),
                       "indicators": {"elderly_ratio": np.array([0., 1.]),
                                      "poverty_rate": np.array([0., 1.]),
                                      "heat_exposure": np.array([0., 1.])}})
    ctx.put("candidates", gpd.GeoDataFrame({"cand_id": ["a", "b"]},
                                            geometry=[Point(0, 0), Point(1, 1)], crs="EPSG:4326"))
    ctx.put("travel", {"matrix": np.array([[1., 20.], [20., 1.]])})
    result = registry.get("compare_value_profiles").fn(
        ctx=ctx, profiles=list(profiles), n_facilities=1, threshold_minutes=15
    )
    assert result.ok and len(result.summary["scenarios"]) == 2
    assert result.summary["scenarios"][0]["selected_ids"] != result.summary["scenarios"][1]["selected_ids"]

def test_action_loop_handles_a_regional_budget_contradiction_before_spatial_work():
    """The selected validator must turn a generic count contradiction directly into HITL."""
    from oasis.contracts.domain import RegionalRequirement
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


def test_timeout_hitl_does_not_restore_model_suggested_parameter_changes():
    @registry.register("timeout_analysis_behavior_test", "Return an unverified timeout.")
    def fail(ctx):
        return ToolResult(ok=False, tool="timeout_analysis_behavior_test",
                          error=ToolError(kind="timeout", message="The requested coverage solve reached its time limit.",
                                          retryable=False))
    llm = RecordingLLM([
        {"kind": "tool", "tool": "timeout_analysis_behavior_test", "rationale": "Test the solver."},
        {"kind": "hitl", "message": "Increase facilities or minutes?",
         "suggested_changes": {"n_facilities": 15, "threshold_minutes": 15}},
    ])
    task = TaskSpec(place_name="Los Angeles", n_facilities=10, threshold_minutes=10)
    result = Orchestrator(llm, {"max_agent_steps": 3, "max_self_correction_rounds": 0}).run(task, explain=False)
    assert result["paused"] and result["recovery"].suggested_changes == {}
    assert "Retry the same task" in result["recovery"].question

def test_hard_coverage_with_travel_requires_feasibility_optimization():
    """A hard target with travel evidence must be tested before the model may stop."""
    orch = Orchestrator(RecordingLLM([]), {})
    orch.ctx.put("travel", {"matrix": [[1.0]]})
    task = TaskSpec(place_name="Phoenix, Arizona, USA", n_facilities=2,
                    threshold_minutes=15, coverage_target=1.0, weights=ValueWeights())
    assert orch._completion_tool(task) == "optimize_sites"

def test_fixed_count_and_minimum_goal_conflict_requires_confirmation():
    """A fixed budget conflicting with a minimum-count goal must not be silently overwritten."""
    llm = RecordingLLM([{"place_name": "San Francisco, California, USA",
                         "analysis_goal": "minimum_facilities", "n_facilities": 1,
                         "threshold_minutes": 15, "coverage_target": 1.0, "weights": None}])
    task = Orchestrator(llm, {}).parse_task(
        "Give me a plan of setting 1 site in San Francisco, and every tract must be reachable within 15 minutes.")
    assert task.analysis_goal == "minimum_facilities"
    assert task.n_facilities == 1 and task.coverage_target == 1.0
    assert any(item.field == "analysis_goal" for item in task.ambiguities)


def test_explicit_minimum_facility_question_keeps_diagnostic_goal():
    """An explicit how-many question must remain an LSCP diagnostic request."""
    llm = RecordingLLM([{"place_name": "San Francisco, California, USA",
                         "analysis_goal": "minimum_facilities", "n_facilities": 1,
                         "threshold_minutes": 15, "coverage_target": 1.0, "weights": None}])
    task = Orchestrator(llm, {}).parse_task("How many sites are needed to cover every tract within 15 minutes?")
    assert task.analysis_goal == "minimum_facilities"


def test_minimal_number_wording_is_not_silently_downgraded():
    llm = RecordingLLM([{
        "place_name": "Washington, District of Columbia, USA",
        "analysis_goal": "minimum_facilities", "threshold_minutes": 15,
        "coverage_target": 1.0,
    }])
    task = Orchestrator(llm, {}).parse_task(
        "What's the minimal number of cooling centers in DC if every tract must be accessible?"
    )
    assert task.analysis_goal == "minimum_facilities"


def test_minimum_and_reachability_goals_have_deterministic_finalizers():
    orch = Orchestrator(RecordingLLM([]), {})
    for handle in ("study", "demand", "candidates", "travel"):
        orch.ctx.put(handle, {})
    assert orch._completion_tool(TaskSpec(
        place_name="Phoenix, Arizona, USA", analysis_goal="minimum_facilities",
    )) == "find_minimum_facilities"
    assert orch._completion_tool(TaskSpec(
        place_name="Phoenix, Arizona, USA", analysis_goal="minimum_threshold",
    )) == "find_minimum_threshold"
    assert orch._completion_tool(TaskSpec(
        place_name="Phoenix, Arizona, USA", analysis_goal="reachability_review",
    )) == "analyze_reachability"


def test_infeasible_minimum_facilities_continues_to_threshold_answer(monkeypatch):
    def minimum_facilities(ctx, **_kwargs):
        summary = {
            "feasible": False, "minimum_facilities": None,
            "threshold_minutes": 15, "coverage_target": 1.0,
            "coverage_group": "all",
        }
        ctx.put("minimum_facilities", summary)
        return ToolResult(
            ok=True, tool="find_minimum_facilities",
            handle="minimum_facilities", summary=summary,
        )

    def minimum_threshold(ctx, **_kwargs):
        summary = {
            "feasible": True, "minimum_threshold_minutes": 42.1,
            "n_facilities": 10, "coverage_target": 1.0,
            "coverage_group": "all",
        }
        ctx.put("minimum_threshold", summary)
        return ToolResult(
            ok=True, tool="find_minimum_threshold",
            handle="minimum_threshold", summary=summary,
        )

    monkeypatch.setattr(registry.get("find_minimum_facilities"), "fn", minimum_facilities)
    monkeypatch.setattr(registry.get("find_minimum_threshold"), "fn", minimum_threshold)
    orch = Orchestrator(RecordingLLM([]), {"max_agent_steps": 4})
    for handle in ("study", "demand", "candidates", "travel"):
        orch.ctx.put(handle, {})
    result = orch.run(TaskSpec(
        place_name="Washington, District of Columbia, USA",
        analysis_goal="minimum_facilities", n_facilities=10,
        threshold_minutes=15, coverage_target=1.0,
    ), explain=False)

    assert result["ok"]
    assert [item.tool for item in result["results"]] == [
        "find_minimum_facilities", "find_minimum_threshold",
    ]
    assert "No number of the retained candidate facilities" in result["message"]
    assert "42.1 minutes by walking" in result["message"]


def test_explicit_null_route_origin_prevents_conceptual_from_fallback():
    llm = RecordingLLM([{
        "place_name": "", "place_names": [],
        "analysis_goal": "facility_siting", "route_origin": None,
    }])
    with pytest.raises(MissingPlaceError) as raised:
        Orchestrator(llm, {}).parse_task(
            "What's the closest cooling facility from a policy perspective?"
        )
    assert raised.value.task_data["route_origin"] is None


def test_route_request_drops_a_model_invented_hard_coverage_target():
    task = Orchestrator(RecordingLLM([{
        "place_name": "Miami Beach, Florida, USA",
        "analysis_goal": "minimum_facilities",
        "coverage_target": 0.9,
        "route_origin": {
            "entity": "Miami Beach", "context": "Florida",
            "positional_hint": "", "raw_text": "From Miami Beach",
            "source": "llm",
        },
    }]), {}).parse_task("From Miami Beach, where is the closest cooling center?")

    assert task.analysis_goal == "facility_siting"
    assert task.coverage_target is None


def test_route_request_replaces_a_destination_misread_as_the_origin():
    task = Orchestrator(RecordingLLM([{
        "place_name": "Miami Beach, Florida, USA",
        "analysis_goal": "reachability_review",
        "route_origin": {
            "entity": "cooling center", "context": "Miami Beach",
            "positional_hint": "", "raw_text": "cooling center, Miami Beach",
            "source": "llm",
        },
    }]), {}).parse_task("From Miami Beach, where's the closest cooling center?")

    assert task.analysis_goal == "facility_siting"
    assert task.route_origin.entity == "Miami Beach"
    assert task.route_origin.source == "fallback"


def test_result_query_completion_does_not_block_minimum_threshold_finalizer():
    task = TaskSpec(
        place_name="Washington, District of Columbia, USA",
        analysis_goal="minimum_facilities", coverage_target=1.0,
        result_queries=[{"metric": "heat_exposure", "limit": 5}],
    )
    orch = Orchestrator(RecordingLLM([]), {})
    for handle in ("study", "demand", "candidates", "travel"):
        orch.ctx.put(handle, {})
    orch.ctx.put("minimum_facilities", {"feasible": False})
    assert orch._completion_tool(task) == "answer_result_queries"
    orch.ctx.put("result_query_answers", {"answers": [{"top_rows": []}]})
    assert orch._completion_tool(task) == "find_minimum_threshold"


def test_route_request_replaces_a_destination_misread_as_the_origin():
    task = Orchestrator(RecordingLLM([{
        "place_name": "Miami Beach, Florida, USA",
        "analysis_goal": "reachability_review",
        "route_origin": {
            "entity": "cooling center", "context": "Miami Beach",
            "positional_hint": "", "raw_text": "cooling center, Miami Beach",
            "source": "llm",
        },
    }]), {}).parse_task("From Miami Beach, where's the closest cooling center?")

    assert task.analysis_goal == "facility_siting"
    assert task.route_origin.entity == "Miami Beach"
    assert task.route_origin.source == "fallback"


def test_result_query_completion_does_not_block_minimum_threshold_finalizer():
    task = TaskSpec(
        place_name="Washington, District of Columbia, USA",
        analysis_goal="minimum_facilities", coverage_target=1.0,
        result_queries=[{"metric": "heat_exposure", "limit": 5}],
    )
    orch = Orchestrator(RecordingLLM([]), {})
    for handle in ("study", "demand", "candidates", "travel"):
        orch.ctx.put(handle, {})
    orch.ctx.put("minimum_facilities", {"feasible": False})
    assert orch._completion_tool(task) == "answer_result_queries"
    orch.ctx.put("result_query_answers", {"answers": [{"top_rows": []}]})
    assert orch._completion_tool(task) == "find_minimum_threshold"


def test_chinese_request_can_use_the_same_typed_schema():
    llm = RecordingLLM([{
        "place_name": "Phoenix, Arizona, USA",
        "place_names": ["Phoenix, Arizona, USA"],
        "analysis_goal": "minimum_facilities",
        "threshold_minutes": 15,
        "coverage_target": 1.0,
    }])
    task = Orchestrator(llm, {}).parse_task(
        "\u51e4\u51f0\u57ce\u5728\u5341\u4e94\u5206\u949f\u6b65\u884c\u8303\u56f4\u5185"
        "\u8986\u76d6\u5168\u90e8\u8857\u533a\u6700\u5c11\u9700\u8981\u51e0\u4e2a"
        "\u964d\u6e29\u4e2d\u5fc3\uff1f"
    )
    assert task.analysis_goal == "minimum_facilities"
    assert task.coverage_target == 1.0


def test_missing_place_error_carries_only_a_valid_converged_task():
    llm = RecordingLLM([{
        "place_name": "", "analysis_goal": "provider_specific_unknown",
        "result_queries": [{"metric": "heat", "direction": "provider_specific_unknown"}],
        "ambiguities": [{"field": "provider_specific_unknown", "question": "Invalid"}],
    }])
    with pytest.raises(MissingPlaceError) as raised:
        Orchestrator(llm, {}).parse_task("Review heat exposure.")
    partial = TaskSpec(**raised.value.task_data)
    assert partial.analysis_goal == "facility_siting"
    assert partial.result_queries[0].direction == "highest"
    assert partial.ambiguities == []

def test_explicit_minute_threshold_overrides_a_missing_llm_field():
    """An explicit minute value is factual input and must not fall back to configuration."""
    llm = RecordingLLM([{"place_name": "Washington, District of Columbia, USA",
                         "analysis_goal": "minimum_facilities", "n_facilities": 8,
                         "threshold_minutes": None, "coverage_target": 1.0, "weights": None}])
    task = Orchestrator(llm, {"accessibility": {"threshold_minutes": 15}}).parse_task(
        "How many sites allow every tract to be reachable within 35 minutes in Washington, District of Columbia, USA?")
    assert task.analysis_goal == "minimum_facilities" and task.threshold_minutes == 35
    assert "threshold_minutes" not in task.notes

def test_heat_review_appends_one_evidence_grounded_llm_paragraph(tmp_path):
    """A completed heat review must append a compact model interpretation to its report."""
    report = tmp_path / "heat_report.md"; report.write_text("# Heat review", encoding="utf-8")
    llm = RecordingLLM([{"decision_summary": "Heat exposure is positively clustered across adjacent tracts.",
                         "self_reflection": "The result describes land-surface temperature and does not establish causation."}])
    orch = Orchestrator(llm, {"max_agent_steps": 2})
    orch.ctx.put("heat_summary", {"statistics": {"morans_i": .42, "permutation_p_value": .01,
                                                    "permutations": 199, "tracts": 20},
                                  "top_tracts": [{"GEOID": "001", "heat_exposure": 42.0}],
                                  "report": str(report)})
    task = TaskSpec(place_name="Phoenix, Arizona, USA", analysis_goal="heat_review")
    result = orch.run(task)
    text = report.read_text(encoding="utf-8")
    assert result["ok"] and result["narrative"].decision_summary
    assert "## Agent interpretation" in text and "does not establish causation" in text
    assert "geometry" not in llm.calls[0]["user"] and '"morans_i": 0.42' in llm.calls[0]["user"]

def test_parser_keeps_llm_recommendations_separate_from_confirmed_values():
    profiles = {
        "balanced": {"label": "Balanced", "weights": {
            "population_density": .25, "elderly_ratio": .25,
            "poverty_rate": .25, "heat_exposure": .25,
        }}
    }
    llm = RecordingLLM([{
        "place_name": "Austin, Texas, USA",
        "weights": profiles["balanced"]["weights"],
        "mobility_scenario": "driving",
        "recommended_mobility_scenario": "walk_drive_equity",
        "recommendation_rationale": "The request mentions households without cars.",
    }])

    task = Orchestrator(llm, {"value_profiles": profiles}).parse_task(
        "Prioritize low-income older residents in Austin."
    )

    assert task.weights is None
    assert task.mobility_scenario == "driving"
    assert task.recommended_value_profile == "balanced"
    assert task.recommended_mobility_scenario == "walk_drive_equity"


def test_partial_profile_weights_match_numerically_with_defaulted_zero_field():
    profiles = {
        "balanced": {"label": "Balanced", "weights": {
            "population_density": .4, "elderly_ratio": .2,
            "poverty_rate": .2, "heat_exposure": .2, "environment_burden": 0.0,
        }}
    }
    llm = RecordingLLM([{
        "place_name": "Austin, Texas, USA",
        "weights": {"population_density": .4, "elderly_ratio": .2,
                    "poverty_rate": .2, "heat_exposure": .2},
    }])

    task = Orchestrator(llm, {"value_profiles": profiles}).parse_task("Review Austin cooling access.")

    assert task.weights is None
    assert task.recommended_value_profile == "balanced"


def test_named_profile_is_recommended_but_not_silently_confirmed():
    profiles = {
        "balanced": {"label": "Balanced", "weights": {
            "population_density": .4, "elderly_ratio": .2,
            "poverty_rate": .2, "heat_exposure": .2, "environment_burden": 0.0,
        }},
        "equity_first": {"label": "Equity first", "weights": ValueWeights().model_dump()},
    }
    deliberate = Orchestrator(
        RecordingLLM([{"place_name": "Phoenix, Arizona, USA"}]),
        {"value_profiles": profiles},
    ).parse_task("Use the balanced profile for Phoenix.")
    generic = Orchestrator(
        RecordingLLM([{"place_name": "Phoenix, Arizona, USA"}]),
        {"value_profiles": profiles},
    ).parse_task("Give me a balanced view of Phoenix.")
    negated = Orchestrator(
        RecordingLLM([{"place_name": "Phoenix, Arizona, USA"}]),
        {"value_profiles": profiles},
    ).parse_task("Do not use equity first for Phoenix.")

    assert deliberate.recommended_value_profile == "balanced"
    assert deliberate.weights is None and not deliberate.is_complete()
    assert generic.recommended_value_profile is None
    assert negated.recommended_value_profile is None


def test_percentage_profile_weights_match_with_numeric_tolerance():
    profiles = {
        "balanced": {"label": "Balanced", "weights": {
            "population_density": .4, "elderly_ratio": .2,
            "poverty_rate": .2, "heat_exposure": .2, "environment_burden": 0.0,
        }}
    }
    task = Orchestrator(RecordingLLM([{
        "place_name": "Austin, Texas, USA",
        "weights": {"population_density": 40, "elderly_ratio": 20,
                    "poverty_rate": 20, "heat_exposure": 20},
    }]), {"value_profiles": profiles}).parse_task("Review Austin cooling access.")

    assert task.recommended_value_profile == "balanced"
    assert task.weights is None


def test_route_origin_preserves_confirmed_candidate_source_and_types():
    task = Orchestrator(RecordingLLM([{
        "place_name": "Phoenix, Arizona, USA",
        "candidate_source": "csv",
        "candidate_amenities": ["library"],
        "route_origin": {
            "entity": "Papago Park", "context": "Phoenix, Arizona",
            "positional_hint": "", "raw_text": "Papago Park", "source": "llm",
        },
    }]), {"optimization": {"n_facilities": 10}}).parse_task(
        "From Papago Park, route to the nearest supplied candidate in Phoenix."
    )

    assert task.candidate_source == "csv"
    assert task.candidate_amenities == ["library"]


@pytest.mark.parametrize("goal", ["sensitivity_analysis", "scenario_comparison"])
def test_multistage_analysis_goals_have_deterministic_dependencies(goal):
    task = TaskSpec(
        place_name="Phoenix, Arizona, USA", analysis_goal=goal,
        sensitivity_thresholds=[10], sensitivity_facility_counts=[5],
        comparison_profiles=["efficiency_first", "equity_first"],
    )
    orch = Orchestrator(RecordingLLM([]), {})
    assert orch._completion_tool(task) == "resolve_study_area"
    orch.ctx.put("study", {})
    assert orch._completion_tool(task) == "prepare_demand"
    orch.ctx.put("demand", {})
    assert orch._completion_tool(task) == "fetch_candidate_sites"
    orch.ctx.put("candidates", {})
    assert orch._completion_tool(task) == "build_travel_matrix"
    orch.ctx.put("travel", {})
    expected = "run_sensitivity_analysis" if goal == "sensitivity_analysis" else "prepare_built_environment"
    assert orch._completion_tool(task) == expected


def test_empty_tract_review_stops_before_the_agent_loop():
    orch = Orchestrator(RecordingLLM([]), {})
    result = orch.run(TaskSpec(
        place_name="Phoenix, Arizona, USA", analysis_goal="tract_review", result_queries=[],
    ), explain=False)
    assert result["paused"] and not result["ok"]
    assert result["plan"].steps == []
    assert "result metric" in result["message"]


def test_parser_recognizes_reachability_by_cars_as_driving():
    """Explicit natural-language car access must override a mistaken walking model value."""
    llm = RecordingLLM([{
        "place_name": "Phoenix, Arizona, USA",
        "analysis_goal": "facility_siting",
        "n_facilities": 2,
        "threshold_minutes": 10,
        "coverage_target": 1.0,
        "mobility_scenario": "walking",
    }])

    task = Orchestrator(llm, {}).parse_task(
        "The budget allows only 2 cooling centers in Phoenix, but every tract "
        "must be reachable by cars within 10 minutes."
    )

    assert task.mobility_scenario == "driving"
    assert task.n_facilities == 2 and task.threshold_minutes == 10
    assert task.coverage_target == 1.0


def test_parser_normalizes_blank_optional_recommendations():
    """Blank model fields should behave like omitted optional values, not fail validation."""
    llm = RecordingLLM([{
        "place_name": "Phoenix, Arizona, USA",
        "mobility_scenario": "walking",
        "recommended_mobility_scenario": "",
        "recommended_value_profile": "",
        "operating_window": "",
        "recommended_sensitivity_thresholds": "",
    }])

    task = Orchestrator(llm, {}).parse_task(
        "Site 8 cooling centers in Phoenix, Arizona within a 15-minute walking threshold."
    )

    assert task.recommended_mobility_scenario is None
    assert task.recommended_value_profile is None
    assert task.operating_window is None
    assert task.recommended_sensitivity_thresholds == []


def test_sensitivity_action_uses_only_confirmed_task_values():
    llm = RecordingLLM([{
        "kind": "tool", "tool": "run_sensitivity_analysis",
        "args": {"threshold_minutes": [60], "facility_counts": [99]},
        "rationale": "Run confirmed what-if experiments.",
    }])
    task = TaskSpec(
        place_name="Austin, Texas, USA", analysis_goal="sensitivity_analysis",
        sensitivity_thresholds=[10, 15], sensitivity_facility_counts=[2, 3],
        weights=ValueWeights(),
    )

    action = Orchestrator(llm, {}).decide_next(task, [], 1)

    assert action.args["threshold_minutes"] == [10.0, 15.0]
    assert action.args["facility_counts"] == [2, 3]


def test_sensitivity_tool_solves_the_confirmed_grid():
    ctx = Context()
    candidates = gpd.GeoDataFrame(
        {"cand_id": ["a", "b"]}, geometry=[Point(0, 0), Point(1, 1)], crs="EPSG:4326"
    )
    demand = {
        "population": np.array([100.0, 100.0]),
        "vulnerable_mask": np.array([True, False]),
        "indicators": {
            "population_density": np.array([1.0, 1.0]),
            "elderly_ratio": np.array([.2, .1]),
            "poverty_rate": np.array([.4, .1]),
            "heat_exposure": np.array([40.0, 39.0]),
        },
    }
    task = TaskSpec(
        place_name="Test City", analysis_goal="sensitivity_analysis",
        sensitivity_thresholds=[5, 10], sensitivity_facility_counts=[1, 2],
        weights=ValueWeights(),
    )
    ctx.put("task", task)
    ctx.put("config", {"optimization": {"solver_time_limit_s": 10}})
    ctx.put("demand", demand)
    ctx.put("candidates", candidates)
    ctx.put("travel", {
        "matrix": np.array([[4.0, 20.0], [20.0, 4.0]]),
        "router": "haversine",
        "routing_report": {"router": "haversine", "policy_minutes": 10},
    })

    result = registry.get("run_sensitivity_analysis").fn(
        ctx=ctx, threshold_minutes=[5, 10], facility_counts=[1, 2],
        weights=ValueWeights().model_dump(),
    )

    assert result.ok and result.summary["experiment_count"] == 4
    assert ctx.has("sensitivity_results")
    assert {row["n_facilities"] for row in result.summary["experiments"]} == {1, 2}


def test_sensitivity_rejects_a_threshold_outside_the_actual_routed_range():
    ctx = Context()
    ctx.put("demand", {
        "population": np.array([100.0]),
        "vulnerable_mask": np.array([False]),
        "indicators": {"population_density": np.array([1.0])},
    })
    ctx.put("candidates", gpd.GeoDataFrame(
        {"cand_id": ["a"]}, geometry=[Point(0, 0)], crs="EPSG:4326",
    ))
    ctx.put("travel", {
        "matrix": np.array([[5.0]]), "router": "haversine",
        "routing_report": {"router": "haversine", "policy_minutes": 10},
    })

    result = registry.get("run_sensitivity_analysis").fn(
        ctx=ctx, threshold_minutes=[15], facility_counts=[1],
    )

    assert not result.ok
    assert "confirmed routed range" in result.error.message

def test_sensitivity_interpretation_receives_mobility_hours_and_provenance():
    llm = RecordingLLM([{
        "decision_summary": "Coverage changed across the confirmed experiment grid.",
        "self_reflection": "Opening hours and public access remain partly unknown.",
    }])
    orch = Orchestrator(llm, {"max_agent_steps": 2})
    orch.ctx.put("sensitivity_results", {
        "baseline": {"covered_population_ratio": .5},
        "experiments": [{"n_facilities": 2, "threshold_minutes": 15,
                         "covered_population_ratio": .5}],
    })
    orch.ctx.put("travel", {"routing_report": {
        "router": "ors_multimodal", "mobility_scenario": "walk_drive_equity",
        "mode_population_share": {"walking": .3, "driving": .7},
    }})
    orch.ctx.put("quality", {
        "operating_hours": {"available_candidates": 8, "unknown_candidates": 3},
        "public_access": {"unknown_access_candidates": 2},
    })
    orch.ctx.lineage.append(Provenance(source="test", tier="cache", record_count=2))
    task = TaskSpec(
        place_name="Austin", analysis_goal="sensitivity_analysis",
        mobility_scenario="walk_drive_equity", sensitivity_thresholds=[15],
        sensitivity_facility_counts=[2],
    )

    result = orch.run(task)

    evidence = llm.calls[0]["user"]
    assert result["ok"] and result["narrative"] is not None
    assert '"mobility_scenario": "walk_drive_equity"' in evidence
    assert '"unknown_candidates": 3' in evidence
    assert '"tier": "cache"' in evidence


def test_semantic_age_recommendation_uses_shared_registry_without_executing_weights():
    from oasis.settings import load_profiles
    profiles = load_profiles()
    llm = RecordingLLM([{"place_name": "Phoenix", "n_facilities": 10, "threshold_minutes": 15,
                         "recommended_value_profile": "older_adults", "weights": None,
                         "recommendation_rationale": "Prioritize the elderly: age-related vulnerability."}])
    task = Orchestrator(llm, {"value_profiles": profiles}).parse_task(
        "Select 10 facilities in Phoenix within 15 minutes. Prioritize the elderly.")
    assert task.recommended_value_profile == "older_adults"
    assert task.weights is None
    assert task.coverage_group == "all"
    assert task.n_facilities == 10 and task.threshold_minutes == 15
    assert 'older_adults' in llm.calls[0]["user"]
    assert profiles['older_adults']['weights']['elderly_ratio'] == .6


def test_valid_semantic_recommendation_is_not_overwritten_by_one_mentioned_group():
    from oasis.settings import load_profiles
    llm = RecordingLLM([{"place_name": "Phoenix", "recommended_value_profile": "equity_first", "weights": None}])
    task = Orchestrator(llm, {"value_profiles": load_profiles()}).parse_task(
        "Select 10 facilities in Phoenix. Prioritize older adults, poverty and heat together, not age alone.")
    assert task.recommended_value_profile == "equity_first"


def test_partial_threshold_recovery_does_not_claim_a_proven_minimum():
    orch = Orchestrator(RecordingLLM([]), {})
    orch.ctx.put("minimum_threshold", {
        "feasible": True, "minimum_threshold_minutes": 104.0,
        "search_complete": False,
    })
    recovery = orch._failure_recovery(TaskSpec(place_name="Phoenix", n_facilities=10), [])
    assert "feasible threshold of 104.0" in recovery.diagnosis
    assert "minimum not proven" in recovery.diagnosis
    assert "at least 104.0" not in recovery.diagnosis


def test_facility_diagnostic_preserves_feasible_without_claiming_minimum(monkeypatch):
    import oasis.services.workflow as workflow
    ctx = Context()
    ctx.put("demand", {"population": np.ones(2), "vulnerable_mask": np.zeros(2, bool)})
    ctx.put("candidates", pd.DataFrame({"cand_id": ["a", "b"]}))
    ctx.put("travel", {"matrix": np.array([[1., 10.], [10., 1.]])})
    ctx.put("task", TaskSpec(place_name="Phoenix", n_facilities=1))
    monkeypatch.setattr(workflow, "solve_lscp", lambda *args, **kwargs:
                        SimpleNamespace(solver_status="Feasible", selected_ids=["a", "b"]))
    result = workflow.find_minimum_facilities(ctx)
    assert result.ok and result.summary["feasible"]
    assert result.summary["minimum_facilities"] is None
    assert result.summary["feasible_facility_count"] == 2
    assert result.summary["minimum_proven"] is False
