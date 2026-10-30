"""Opt-in placement experiments must stay separate from observed-site evidence."""
import itertools
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from shapely.geometry import Point, box

from oasis.agent.context import Context
from oasis.agent.orchestrator import Orchestrator
from oasis.contracts.domain import TaskSpec
from oasis.core.spatial.accessibility import haversine_matrix
from oasis.services.workflow import find_minimum_facilities
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
    {"analysis_goal": "facility_siting"}, {"diagnostic_fixed_inputs": ["n_facilities", "threshold_minutes"]},
    {"facility_capacity": 100}, {"mobility_scenario": "walk_drive_equity"},
    {"regional_requirements": [{"region": "North", "minimum_facilities": 1}]},
])
def test_extension_does_not_silently_drop_unsupported_constraints(changes):
    with pytest.raises(ValidationError):
        TaskSpec(**{"analysis_goal": "minimum_facilities", "allow_hypothetical_sites": True, **changes})
