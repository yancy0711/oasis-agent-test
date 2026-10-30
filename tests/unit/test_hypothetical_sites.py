"""Opt-in placement experiments must stay separate from observed-site evidence."""
import itertools
import json
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from shapely.geometry import Point, box

from oasis.agent.context import Context
from oasis.agent.orchestrator import Orchestrator
from oasis.contracts.domain import TaskSpec, ValueWeights, ToolError
from oasis.core.spatial.accessibility import haversine_matrix
from oasis.services.workflow import find_minimum_facilities
from oasis.services import workflow
from oasis.services.application import OasisService


def context(tmp_path, enabled=True):
    ctx = Context()
    points = gpd.GeoDataFrame(geometry=[Point(0, 0), Point(.001, 0), Point(.05, 0)], crs=4326)
    areas = gpd.GeoDataFrame({"GEOID": ["a", "b", "c"], "heat_exposure": [30, 31, 32]},
                             geometry=[box(p.x-.0001, -.0001, p.x+.0001, .0001) for p in points.geometry], crs=4326)
    sites = gpd.GeoDataFrame({"cand_id": ["existing"], "name": ["Library"]}, geometry=[Point(0, 0)], crs=4326)
    ctx.put("task", TaskSpec(place_name="Test", analysis_goal="minimum_facilities", threshold_minutes=2,
                             coverage_target=1, allow_hypothetical_sites=enabled))
    ctx.put("config", {"output": {"dir": str(tmp_path)}, "optimization": {"solver_time_limit_s": 10}})
    ctx.put("study", {"cache_scope": "test", "boundary": gpd.GeoDataFrame(geometry=[box(-1, -1, 1, 1)], crs=4326)})
    ctx.put("demand", {"points": points, "areas": areas, "population": np.ones(3), "vulnerable_mask": np.ones(3, bool)})
    ctx.put("candidates", sites)
    ctx.put("travel", {"matrix": haversine_matrix(points, sites, 4.8, 1.3)})
    return ctx


def test_opt_in_returns_separate_verified_finite_set_minimum_and_labeled_map(tmp_path, monkeypatch):
    monkeypatch.setattr("requests.sessions.Session.request", lambda *a, **k: pytest.fail("Extension must stay offline"))
    ctx = context(tmp_path)
    original_sites, original_matrix = ctx.get("candidates").copy(), ctx.get("travel")["matrix"].copy()
    orch = Orchestrator(None, ctx.get("config"))
    orch.ctx = ctx
    result = orch.run(ctx.get("task"), explain=False)
    assert result["ok"] and not result["diagnostics"]["minimum_facilities"]["feasible"]
    expansion = result["diagnostics"]["hypothetical_coverage"]
    assert expansion["feasible"] and expansion["minimum_proven"]
    assert expansion["minimum_facilities"] == 2
    assert expansion["hypothetical_selected_count"] >= 1
    assert expansion["accuracy"] == "approximate_haversine" and expansion["threshold_minutes"] == 2
    assert not ctx.has("coverage_alternative")
    pd.testing.assert_frame_equal(original_sites, ctx.get("candidates"))
    np.testing.assert_array_equal(original_matrix, ctx.get("travel")["matrix"])
    # Independent enumeration of the tiny finite set checks the actual optimum.
    points = ctx.get("demand")["points"]
    combined = gpd.GeoDataFrame(geometry=[Point(0, 0), *points.geometry], crs=4326)
    cover = haversine_matrix(points, combined, 4.8, 1.3) <= 2
    optimum = next(k for k in range(1, 5) if any(cover[:, subset].any(axis=1).all() for subset in itertools.combinations(range(4), k)))
    assert expansion["minimum_facilities"] == optimum
    features = json.loads(Path(expansion["map"]).read_text(encoding="utf-8"))["features"]
    synthetic = [f for f in features if f["properties"].get("is_hypothetical")]
    assert len(synthetic) == expansion["hypothetical_selected_count"]
    assert all("Hypothetical" in f["properties"]["name"] for f in synthetic)
    assert "Post-paper hypothetical-site extension" in result["message"]
    service = object.__new__(OasisService)
    manifest = service._artifacts("extension-test", result, tmp_path)
    assert manifest["hypothetical_coverage_map"] == Path(expansion["map"]).resolve()
    report = manifest["report"].read_text(encoding="utf-8")
    assert "Post-paper extension: hypothetical sites" in report
    assert "not ORS-verified" in report


def test_default_keeps_original_workflow(tmp_path):
    assert not TaskSpec().allow_hypothetical_sites
    ctx = context(tmp_path, enabled=False)
    find_minimum_facilities(ctx, threshold_minutes=2)
    assert not ctx.has("hypothetical_coverage")
    assert ctx.has("coverage_alternative")


@pytest.mark.parametrize("changes", [
    {"analysis_goal": "minimum_threshold"}, {"diagnostic_fixed_inputs": ["n_facilities", "threshold_minutes"]},
    {"facility_capacity": 100}, {"mobility_scenario": "walk_drive_equity"},
    {"equity_alpha": .5},
    {"regional_requirements": [{"region": "North", "minimum_facilities": 1}]},
])
def test_extension_does_not_silently_drop_unsupported_constraints(changes):
    with pytest.raises(ValidationError):
        TaskSpec(**{"analysis_goal": "minimum_facilities", "allow_hypothetical_sites": True, **changes})


@pytest.mark.parametrize("budget,target,feasible", [(2, 1, True), (1, 1, False), (1, None, True), (1, .9, True)])
def test_fixed_budget_is_not_replaced_by_a_minimum_count_question(tmp_path, monkeypatch, budget, target, feasible):
    monkeypatch.setattr("requests.sessions.Session.request", lambda *a, **k: pytest.fail("No network in the expanded solve"))
    ctx = context(tmp_path)
    if target == .9:
        ctx.get("demand")["population"] = np.array([90., 5., 5.])
    task = TaskSpec(place_name="Test", analysis_goal="facility_siting", n_facilities=budget, threshold_minutes=2,
                    coverage_target=target, allow_hypothetical_sites=True, weights=ValueWeights())
    original_candidates = ctx.get("candidates").copy()
    original_matrix = ctx.get("travel")["matrix"].copy()
    orch = Orchestrator(None, ctx.get("config"))
    orch.ctx = ctx
    result = orch.run(task, explain=False)
    assert result["ok"] is feasible
    assert [item.tool for item in result["results"]] == ["plan_hypothetical_portfolio"]
    extension = result["diagnostics"]["hypothetical_coverage"]
    assert extension["feasible"] is feasible
    assert extension["planning_mode"] == "fixed_budget" and extension["facility_budget"] == budget
    assert extension["threshold_minutes"] == 2 and extension["coverage_target"] == target
    assert extension["minimum_facilities"] is None and not extension["minimum_proven"]
    assert extension["value_weights"] == task.weights.model_dump(mode="json")
    assert ctx.get("task") == task
    pd.testing.assert_frame_equal(original_candidates, ctx.get("candidates"))
    np.testing.assert_array_equal(original_matrix, ctx.get("travel")["matrix"])
    if feasible:
        assert result["recovery"] is None
        assert extension["feasible_facility_count"] <= budget
        assert extension["hypothetical_selected_count"] > 0
        assert extension["metrics"]["coverage"] >= (target or 0) - 1e-8
        assert Path(extension["map"]).is_file()
        assert "unchanged budget" in result["message"]
    else:
        assert extension["solver_status"] == "Infeasible" and "map" not in extension
        assert "expanded finite candidate set" in result["message"]
        assert result["paused"] and result["recovery"] is not None
        assert result["recovery"].suggested_changes == {"n_facilities": 2}
        assert all(len(option["changes"]) <= 1 for option in result["recovery"].alternatives)
        assert "coverage_target" not in result["recovery"].suggested_changes


def test_fixed_budget_auto_runs_once_and_exposes_approximate_provenance(tmp_path, monkeypatch):
    template = context(tmp_path)
    class PreparedOrchestrator(Orchestrator):
        def __init__(self, llm, cfg):
            super().__init__(llm, cfg)
            for key in ("study", "demand", "candidates"):
                self.ctx.put(key, template.get(key))
    monkeypatch.setattr("oasis.services.application.Orchestrator", PreparedOrchestrator)
    cfg = {"llm": {}, "accessibility": {}, "optimization": {"solver_time_limit_s": 5},
           "runtime": {"run_db": str(tmp_path / "runs.sqlite")}, "output": {"dir": str(tmp_path / "outputs")}}
    service = OasisService(cfg=cfg, llm_factory=lambda: None)
    task = TaskSpec(place_name="Test", n_facilities=2, threshold_minutes=2, coverage_target=1,
                    allow_hypothetical_sites=True, weights=ValueWeights())
    try:
        with pytest.raises(ValueError, match="approximate only"):
            service.start_run(task, "precise")
        accepted = service.start_run(task, "auto")
        assert accepted["mode"] == "quick"
        deadline = time.monotonic() + 10
        while True:
            snapshot = service.run_snapshot(accepted["run_id"])
            if snapshot["status"] not in {"queued", "running"}:
                break
            assert time.monotonic() < deadline
            time.sleep(.02)
        assert snapshot["status"] == "succeeded", snapshot.get("error")
        result = snapshot["result"]
        assert result["execution"]["phase"] == "quick" and result["execution"]["accuracy"] == "approximate"
        assert result["execution"]["router_used"] == "haversine"
        assert snapshot["preview_result"] is None
        assert "hypothetical_coverage_map" in result["artifacts"] and result["metrics"]["coverage"] == pytest.approx(1)
        report = service.artifact(accepted["run_id"], "report").read_text(encoding="utf-8")
        assert "No ORS refinement is scheduled" in report and "Wait for ORS refinement" not in report
    finally:
        service.executor.shutdown(wait=True)


def test_successful_empty_osm_lookup_can_expand_but_provider_errors_still_stop(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    ctx.put("task", TaskSpec(place_name="Test", n_facilities=2, threshold_minutes=2, coverage_target=1,
                             allow_hypothetical_sites=True, weights=ValueWeights()))
    ctx.get("study")["cache_scope"] = "test"
    monkeypatch.setattr(workflow, "fetch_candidates", lambda *a, **k: (None, ToolError(kind="not_found", message="No facilities")))
    empty = workflow.fetch_candidate_sites(ctx, ["library"])
    assert empty.ok and len(ctx.get("candidates")) == 0
    planned = workflow.plan_hypothetical_portfolio(ctx)
    assert planned.ok and planned.summary["feasible"]
    assert planned.summary["existing_selected_count"] == 0 and planned.summary["hypothetical_selected_count"] == 2
    monkeypatch.setattr(workflow, "fetch_candidates", lambda *a, **k: (None, ToolError(kind="unknown", message="Provider unavailable")))
    failed = workflow.fetch_candidate_sites(ctx, ["library"])
    assert not failed.ok and failed.error.message == "Provider unavailable"
