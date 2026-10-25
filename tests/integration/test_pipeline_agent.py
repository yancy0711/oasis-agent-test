"""Offline coverage for the formal pipeline, tool chain, and FakeLLM agent."""
import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Point, box

import cli.main as main
from oasis.agent.context import Context
from oasis.agent.orchestrator import Orchestrator
from oasis.contracts.domain import PlanStep, Provenance, TaskSpec, ToolError, ToolResult
from oasis.settings import get_weights
from cli.main import run_agent, run_pipeline
from oasis.core.data import geocoding
from oasis.services.registry import registry
from oasis.services import workflow


class FakeLLM:
    """Return canned structured responses without any network call."""
    def __init__(self, canned):
        """Store the ordered canned response queue."""
        self.canned = list(canned)

    def structured(self, system, user, schema=None):
        """Return and remove the next canned structured response."""
        return self.canned.pop(0)


def test_resolve_fips_uses_place_representative_point(monkeypatch):
    """Verify place geometry is converted into a coordinate FIPS lookup."""
    boundary = gpd.GeoDataFrame(geometry=[box(-118.3, 33.9, -118.1, 34.1)], crs="EPSG:4326")
    seen = {}
    monkeypatch.setattr(geocoding, "resolve_place", lambda *args: boundary)
    monkeypatch.setattr(geocoding, "_coordinate_fips", lambda lon, lat: seen.update(lon=lon, lat=lat) or
                        {"state_fips": "06", "county_fips": "037"})
    assert geocoding.resolve_fips("Los Angeles County") == {"state_fips": "06", "county_fips": "037"}
    assert -118.3 <= seen["lon"] <= -118.1 and 33.9 <= seen["lat"] <= 34.1


def test_context_accepts_multiple_provenance_records():
    """Verify Context stores objects and multiple lineage records together."""
    ctx = Context(); records = [Provenance(source="a"), Provenance(source="b")]
    ctx.put("x", object(), records)
    assert ctx.has("x") and ctx.get("missing", None) is None and ctx.lineage == records


def test_fake_llm_parse_and_self_correction():
    '''This test simulates a user prompt -> LLM parse -> HITL clarification -> plan -> execute, with a single tool that fails and is repaired by the LLM.'''
    parsed = FakeLLM([{"place_name": "Phoenix, Arizona, USA", "n_facilities": 10,
                       "weights": None, "notes": "ask user"}])
    task = Orchestrator(parsed, {"max_self_correction_rounds": 1}).parse_task("help underserved Phoenix")
    assert isinstance(task, TaskSpec) and not task.is_complete()

    calls = []
    @registry.register("flaky_tool_test", "offline repair test")
    def flaky(ctx, **kwargs):
        """Fail once so the orchestrator must apply the canned repair."""
        calls.append(kwargs)
        if len(calls) == 1:
            return ToolResult(ok=False, tool="flaky_tool_test",
                              error=ToolError(kind="not_found", message="empty", hint="broaden"))
        return ToolResult(ok=True, tool="flaky_tool_test")

    repaired = FakeLLM([{"tool": "flaky_tool_test", "args": {"amenities": ["library", "school"]}}])
    result = Orchestrator(repaired, {"max_self_correction_rounds": 1}).execute(
        PlanStep(tool="flaky_tool_test", args={"amenities": ["library"]}))
    assert result.ok and len(calls) == 2


def test_agent_parser_requires_a_nonempty_place():
    """A missing LLM place must stop before planning or tool execution."""
    fake = FakeLLM([{"n_facilities": 8, "threshold_minutes": 15, "weights": None}])
    with pytest.raises(ValueError, match="Could not parse a place"):
        Orchestrator(fake, {}).parse_task("Site cooling centers somewhere")


def test_agent_prompts_for_a_missing_place_without_reparsing(monkeypatch):
    """Interactive recovery must preserve parsed fields and use one parse call."""
    @registry.register("capture_recovered_place_test", "capture recovered place")
    def capture(ctx):
        """Return the place stored after interactive recovery."""
        ctx.put("outputs", {})
        return ToolResult(ok=True, tool="capture_recovered_place_test",
                          summary={"place": ctx.get("task").place_name})

    fake = FakeLLM([
        {"n_facilities": "some", "threshold_minutes": 15, "weights": None},
        {"kind": "tool", "tool": "capture_recovered_place_test"},
    ])
    monkeypatch.setattr("builtins.input", lambda _: "DC")
    monkeypatch.setattr(main.hitl, "clarify_place", lambda task, **_: task)
    result = run_agent({"llm": {"model": "fake", "max_self_correction_rounds": 0}},
                       "site some centers", llm=fake, interactive=True, profile="efficiency_first")
    assert result["ok"] and result["task"].place_name == "DC"
    assert result["task"].n_facilities == 10 and "place supplied by user" in result["task"].notes
    assert fake.canned == []


def test_agent_parser_uses_runtime_defaults_for_vague_numbers():
    """Vague or missing quantities must resolve to configured numeric defaults."""
    fake = FakeLLM([{"place_name": "DC", "n_facilities": "some",
                     "threshold_minutes": None, "weights": None}])
    cfg = {"optimization": {"n_facilities": 7}, "accessibility": {"threshold_minutes": 12}}
    task = Orchestrator(fake, cfg).parse_task("Site some cooling centers in DC")
    assert task.n_facilities == 7 and task.threshold_minutes == 12
    assert "invalid n_facilities 'some' replaced with 7" in task.notes
    assert "runtime defaults" not in task.notes


def test_agent_parser_rejects_unregistered_llm_weights():
    """LLM weights outside registered profiles must return to HITL."""
    fake = FakeLLM([{"place_name": "DC", "n_facilities": 8, "threshold_minutes": 15,
                     "weights": {"population_density": .7, "elderly_ratio": .1,
                                 "poverty_rate": .1, "heat_exposure": .1}}])
    cfg = {"value_profiles": {"efficiency_first": {
        "weights": {"population_density": 1., "elderly_ratio": 0., "poverty_rate": 0., "heat_exposure": 0.}}}}
    task = Orchestrator(fake, cfg).parse_task("maximize population coverage")
    assert task.weights is None and "unregistered weights require HITL" in task.notes


def test_agent_plan_normalizes_common_json_key_aliases():
    """Common model key aliases must normalize before Pydantic validation."""
    fake = FakeLLM([{"plan": [{"tool_name": "resolve_study_area",
                                "arguments": {"year": 2023}}]}])
    task = TaskSpec(place_name="New York, New York, USA")
    plan = Orchestrator(fake, {}).plan(task)
    assert plan.steps[0].tool == "resolve_study_area"
    assert plan.steps[0].args == {"place_name": task.place_name}


def test_agent_plan_drops_internal_context_handles():
    """Plans may use only arguments exposed by each high-level tool."""
    fake = FakeLLM([{"steps": [{"tool": "prepare_demand", "args": {
        "study_handle": "Washington_DC_2023", "start_date": "2022-06-01"}}]}])
    plan = Orchestrator(fake, {}).plan(TaskSpec(place_name="Washington, DC"))
    assert plan.steps[0].args == {}


def test_agent_plan_binds_confirmed_task_arguments():
    """Required tool arguments must come from the confirmed TaskSpec."""
    fake = FakeLLM([{"steps": [
        {"tool": "resolve_study_area", "args": {}},
        {"tool": "fetch_candidate_sites", "args": {}},
        {"tool": "optimize_sites", "args": {"n_facilities": 99}},
    ]}])
    task = TaskSpec(place_name="Washington, District of Columbia, USA", n_facilities=8,
                    threshold_minutes=15, weights=get_weights("efficiency_first"))
    steps = Orchestrator(fake, {}).plan(task).steps
    assert steps[0].args == {"place_name": task.place_name}
    assert steps[1].args == {"amenities": task.candidate_amenities}
    assert steps[2].args == {"n_facilities": 8, "threshold_minutes": 15,
                             "weights": task.weights.model_dump(), "coverage_target": None,
                             "coverage_group": "all", "facility_capacity": None,
                             "equity_alpha": None}


def test_agent_stops_after_unrecoverable_step():
    """A terminal tool failure must stop dependent plan steps immediately."""
    called = []

    @registry.register("agent_fail_stop_test", "terminal failure")
    def fail(ctx):
        """Return one non-retryable failure for fail-fast verification."""
        return ToolResult(ok=False, tool="agent_fail_stop_test",
                          error=ToolError(kind="not_found", message="missing", retryable=False))

    @registry.register("agent_should_not_run_test", "dependent step")
    def should_not_run(ctx):
        """Record an invalid execution after a terminal dependency failure."""
        called.append(True); return ToolResult(ok=True, tool="agent_should_not_run_test")

    fake = FakeLLM([
        {"place_name": "DC", "weights": None},
        {"kind": "tool", "tool": "agent_fail_stop_test"},
        {"kind": "hitl", "message": "The required input is missing."},
    ])
    result = run_agent({"llm": {"model": "fake", "max_self_correction_rounds": 0}},
                       "test", llm=fake, interactive=False, validate_place=False)
    assert not result["ok"] and result["failed_tool"] == "agent_fail_stop_test"
    assert len(result["results"]) == 1 and result["paused"] and not called


def test_run_agent_performs_parse_hitl_observe_and_act():
    """Verify the public agent entry executes parse, HITL, observe, act, and finish."""
    @registry.register("capture_task_test", "capture clarified task")
    def capture(ctx):
        """Confirm the clarified TaskSpec is available through Context."""
        task = ctx.get("task")
        if task.is_complete(): ctx.put("outputs", {})
        return ToolResult(ok=task.is_complete(), tool="capture_task_test", summary={"notes": task.notes})

    fake = FakeLLM([
        {"place_name": "Phoenix, Arizona, USA", "weights": None, "notes": "equity is vague"},
        {"kind": "tool", "tool": "capture_task_test", "rationale": "Verify the confirmed task."},
    ])
    cfg = {"llm": {"model": "fake", "max_self_correction_rounds": 0}}
    result = run_agent(cfg, "help underserved Phoenix", llm=fake, interactive=False, validate_place=False,
                       n_facilities=3, threshold_minutes=12, profile="efficiency_first")
    assert result["task"].is_complete() and "profile=efficiency_first" in result["task"].notes
    assert result["task"].n_facilities == 3 and result["task"].threshold_minutes == 12
    assert result["results"][0].ok


def test_cli_pipeline_matches_manual_seven_tool_chain(monkeypatch, tmp_path):
    """Verify CLI and manual seven-tool execution produce the same solution."""
    geoids = ["06037000100", "06037000200", "06037000300"]
    tracts = gpd.GeoDataFrame({"GEOID": geoids}, geometry=[
        box(-118.30, 34.00, -118.28, 34.02), box(-118.20, 34.00, -118.18, 34.02),
        box(-118.10, 34.00, -118.08, 34.02)], crs="EPSG:4326")
    boundary = gpd.GeoDataFrame(geometry=[box(-118.4, 33.9, -118.0, 34.1)], crs="EPSG:4326")
    raw = pd.DataFrame({"GEOID": geoids, "total_population": [100, 80, 60],
                        "m65_66": [10, 20, 5], "below_poverty": [5, 20, 20],
                        "poverty_universe": [100, 80, 60]})
    heat = pd.DataFrame({"GEOID": geoids, "mean_lst": [30., 35., 40.]})
    candidates = gpd.GeoDataFrame({"cand_id": ["a", "b", "c"]}, geometry=[
        Point(-118.29, 34.01), Point(-118.19, 34.01), Point(-118.09, 34.01)], crs="EPSG:4326")
    monkeypatch.setattr(workflow, "resolve_place", lambda *args: boundary)
    monkeypatch.setattr(workflow, "resolve_fips", lambda *args: {"state_fips": "06", "county_fips": "037"})
    monkeypatch.setattr(workflow, "fetch_acs", lambda *args: raw)
    monkeypatch.setattr(workflow, "fetch_tract_geometry", lambda *args: tracts)
    monkeypatch.setattr(workflow, "fetch_heat_exposure", lambda *args, **kwargs: (heat, Provenance(source="fake heat", record_count=3)))
    monkeypatch.setattr(workflow, "fetch_candidates", lambda *args, **kwargs: (candidates, None))

    cfg = {"llm": {}, "data": {"landsat_environment_indices": {"enabled": False}},
           "accessibility": {"router": "haversine", "threshold_minutes": 15},
           "optimization": {"n_facilities": 1, "solver_time_limit_s": 5},
           "output": {"dir": str(tmp_path / "pipeline")}}
    pipeline = run_pipeline(cfg, "Test County, USA", validate_place=False)
    assert pipeline["ok"] and all((tmp_path / "pipeline" / "test_county_usa" / name).exists()
                                  for name in ["coverage_map.geojson", "pareto.png", "report.md"])

    task = TaskSpec(place_name="Test County, USA", n_facilities=1, weights=get_weights("balanced"))
    ctx = Context(); ctx.put("config", cfg); ctx.put("task", task)
    calls = [
        ("resolve_study_area", {"place_name": task.place_name}), ("prepare_demand", {}),
        ("prepare_built_environment", {}),
        ("fetch_candidate_sites", {"amenities": task.candidate_amenities}), ("build_travel_matrix", {}),
        ("optimize_sites", {"n_facilities": 1, "threshold_minutes": 15,
                            "weights": task.weights.model_dump()}),
        ("generate_outputs", {"output_dir": str(tmp_path / "manual")}),
    ]
    assert all(registry.get(name).fn(ctx=ctx, **args).ok for name, args in calls)
    assert ctx.get("analysis")["solution"].model_dump() == pipeline["solution"].model_dump()


def test_build_travel_matrix_uses_sparse_ors_path(monkeypatch):
    """Verify the formal pipeline reads the sparse ORS pair-cache path."""
    points = gpd.GeoDataFrame(geometry=[Point(-118.20, 34.00), Point(-118.21, 34.01)], crs="EPSG:4326")
    candidates = gpd.GeoDataFrame({"cand_id": ["a"]}, geometry=[Point(-118.205, 34.005)], crs="EPSG:4326")
    cfg = {"accessibility": {
        "router": "ors", "ors_base_url": "https://example.test/matrix",
        "ors_max_policy_threshold_minutes": 20, "ors_pruning_margin": 1.5,
    }}
    ctx = Context(); ctx.put("config", cfg)
    ctx.put("study", {"place_type": "city", "area_km2": 1, "cache_scope": "test_city"})
    ctx.put("demand", {"points": points, "population": np.array([1.0, 1.0]),
                       "vulnerable_mask": np.array([False, False]), "indicators": {}})
    ctx.put("candidates", candidates)
    seen = {}

    def fake_sparse(demand, supply, eligible, **kwargs):
        """Return a cached sparse matrix without making a network request."""
        seen["eligible"] = eligible.copy(); seen["fallback"] = kwargs["fallback_matrix"]
        return np.array([[7.], [8.]]), {
            "router": "ors_sparse", "tier": "cache", "eligible_pairs": int(eligible.sum()),
            "cache_pairs": int(eligible.sum()), "live_pairs": 0,
        }

    monkeypatch.setattr(workflow, "sparse_network_matrix", fake_sparse)
    result = registry.get("build_travel_matrix").fn(ctx=ctx)
    assert result.ok and result.summary["router"] == "ors_sparse"
    assert result.summary["tier"] == "cache" and result.summary["live_pairs"] == 0
    assert ctx.get("travel")["routing_report"]["policy_minutes"] == 15
    assert seen["eligible"].all() and seen["fallback"].shape == (2, 1)
    assert result.provenance.tier == "cache"


def test_pipeline_profile_is_configurable(monkeypatch):
    """Verify YAML selects a profile and the explicit argument overrides it."""
    seen = []

    def fake_chain(cfg, task):
        """Capture the task without executing geospatial tools."""
        seen.append(task); return {"ok": True, "outputs": {}}

    monkeypatch.setattr(main, "run_tool_chain", fake_chain)
    main.run_pipeline({"optimization": {"value_profile": "cooling_first"}}, "Test City, USA", validate_place=False)
    main.run_pipeline({"optimization": {"value_profile": "cooling_first"}}, "Test City, USA", "equity_first",
                      validate_place=False, n_facilities=3, threshold_minutes=12)
    assert seen[0].weights == get_weights("cooling_first") and "cooling_first" in seen[0].notes
    assert seen[1].weights == get_weights("equity_first") and "equity_first" in seen[1].notes
    assert seen[1].n_facilities == 3 and seen[1].threshold_minutes == 12


def test_pipeline_confirms_place_before_tool_chain(monkeypatch):
    """Verify place HITL finishes before any production tool can run."""
    events = []

    def fake_clarify(task, interactive):
        """Record confirmation and return its canonical boundary name."""
        events.append(("hitl", interactive)); task.place_name = "Los Angeles, California, USA"; return task

    def fake_chain(cfg, task):
        """Record the first tool-chain entry after place confirmation."""
        events.append(("tools", task.place_name)); return {"ok": True, "outputs": {}}

    monkeypatch.setattr(main.hitl, "clarify_place", fake_clarify)
    monkeypatch.setattr(main, "run_tool_chain", fake_chain)
    result = main.run_pipeline({}, "LA", interactive=True)
    assert result["ok"]


def test_intersecting_counties_detects_multi_county_city(monkeypatch):
    """Verify one city boundary can resolve to multiple county FIPS codes."""
    boundary = gpd.GeoDataFrame(geometry=[box(0, 0, 2, 1)], crs="EPSG:4326")
    counties = gpd.GeoDataFrame({"COUNTYFP": ["001", "003", "005"]},
                                geometry=[box(0, 0, 1, 1), box(1, 0, 2, 1), box(1.999, 0, 3, 1)], crs="EPSG:4326")
    monkeypatch.setattr(workflow, "fetch_county_geometry", lambda *args: counties)
    assert workflow._intersecting_counties(boundary, "36", 2022, "001") == ["001", "003"]


def test_prepare_demand_combines_multiple_counties(monkeypatch):
    """Verify demand preparation fetches and combines every resolved county."""
    ctx = Context(); ctx.put("study", {"state_fips": "36", "county_fips": "001",
        "county_fips_list": ["001", "003"], "year": 2022, "cache_scope": "test_city",
        "place_type": "city", "boundary": gpd.GeoDataFrame(geometry=[box(-1, -1, 3, 2)], crs="EPSG:4326")})
    def fake_acs(state, county, groups, year):
        """Return one tract row per requested county."""
        return pd.DataFrame({"GEOID": [state + county + "000100"], "total_population": [100],
                             "m65_66": [10], "below_poverty": [5], "poverty_universe": [100]})
    def fake_tracts(state, county, year):
        """Return one tract polygon per requested county."""
        x = 0 if county == "001" else 1
        return gpd.GeoDataFrame({"GEOID": [state + county + "000100"], "ALAND": [100000]}, geometry=[box(x, 0, x + .5, .5)], crs="EPSG:4326")
    monkeypatch.setattr(workflow, "fetch_acs", fake_acs); monkeypatch.setattr(workflow, "fetch_tract_geometry", fake_tracts)
    monkeypatch.setattr(workflow, "fetch_heat_exposure", lambda areas, *args, **kwargs: (
        pd.DataFrame({"GEOID": areas["GEOID"], "mean_lst": [30., 31.]}), Provenance(source="fake heat", record_count=2)))
    result = registry.get("prepare_demand").fn(ctx=ctx)
    assert result.ok and result.summary["tracts"] == 2
    assert ctx.get("demand")["areas"]["population_density"].eq(1000).all()
