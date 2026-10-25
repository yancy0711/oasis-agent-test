"""Planner acquisition keeps OSM geometry semantics with bounded transport."""
from collections import OrderedDict
from copy import deepcopy

import geopandas as gpd
import osmnx as ox
import pytest
import requests
import httpx
from shapely.geometry import Polygon, box

from oasis.core.cancellation import RunCancelled
from oasis.core.data import osm, overpass


def provider_response(payload):
    return httpx.Response(200, json=payload,
                          request=httpx.Request("POST", "https://example.test/api/interpreter"))


@pytest.fixture
def setup_osm(monkeypatch, tmp_path):
    monkeypatch.setattr(osm, "cache_dir", lambda name: tmp_path)
    monkeypatch.setattr(overpass, "_FAILURES", OrderedDict())
    monkeypatch.setattr(overpass, "_RATE_LIMITS", {})
    monkeypatch.delenv("OASIS_OVERPASS_URL", raising=False)
    # All provider calls must be explicit test doubles.
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("Hidden status request"))
    monkeypatch.setattr(overpass, "cancellable_request", lambda *a, **k: pytest.fail("Unmocked provider call"))
    monkeypatch.setattr(ox.settings, "cache_folder", str(tmp_path))
    monkeypatch.setattr(ox.settings, "use_cache", True)
    return gpd.GeoDataFrame(geometry=[box(-117.38, 33.98, -117.37, 33.99)], crs=4326)


def payload():
    return {"elements": [
        {"type": "node", "id": 1, "lon": -117.376, "lat": 33.983},
        {"type": "node", "id": 2, "lon": -117.375, "lat": 33.983},
        {"type": "node", "id": 3, "lon": -117.375, "lat": 33.984},
        {"type": "node", "id": 4, "lon": -117.376, "lat": 33.984},
        {"type": "way", "id": 5, "nodes": [1, 2, 3, 4, 1],
         "tags": {"amenity": "library", "name": "Test library", "building:levels": "2", "capacity": "100"}},
        {"type": "node", "id": 6, "lon": -117.0, "lat": 33.0, "tags": {"amenity": "library"}}
    ]}


def test_bounded_download_keeps_polygon_capacity_boundary_and_cache(setup_osm, monkeypatch):
    calls, progress = [], []
    def post(method, url, **kwargs):
        calls.append(kwargs)
        return provider_response(payload())
    monkeypatch.setattr(overpass, "cancellable_request", post)
    candidates, error = osm.fetch_candidates(setup_osm, ["library"], progress=lambda *a, **k: progress.append(a))
    assert error is None and len(candidates) == 1  # Outside node removed.
    assert candidates.attrs["osm_acquisition"] == {"live_parts": 1, "cache_parts": 0}
    row = candidates.iloc[0]
    assert row["name"] == "Test library" and row.geometry.geom_type == "Point"
    assert row["footprint_area_m2"] > 0
    assert row["gross_floor_area_proxy_m2"] == 2 * row["footprint_area_m2"]
    assert row["capacity_observed_people"] == 100
    assert row["capacity_evidence"] == "observed_osm_capacity"
    query = calls[0]["data"]["data"]
    assert "[timeout:20][maxsize:67108864]" in query
    assert "out center" not in query  # Planner must keep full geometry.
    assert calls[0]["timeout"] == (5, 45)
    assert "OSM area 1/1" in progress[0][0]
    again, _ = osm.fetch_candidates(setup_osm, ["library"])
    assert len(calls) == 1 and again.geometry.equals(candidates.geometry)
    assert again.attrs["osm_acquisition"] == {"live_parts": 0, "cache_parts": 1}


def test_historical_osmnx_cache_is_reused_without_remote_call(setup_osm, monkeypatch):
    from osmnx import _http
    monkeypatch.setattr(_http, "_retrieve_from_cache", lambda url: deepcopy(payload()))
    candidates, error = osm.fetch_candidates(setup_osm, ["library"])
    assert error is None and len(candidates) == 1


def test_incomplete_response_is_not_cached_or_used_for_optimization(setup_osm, monkeypatch, tmp_path):
    incomplete = {**payload(), "remark": "runtime error: timeout"}
    monkeypatch.setattr(overpass, "cancellable_request", lambda *a, **k: provider_response(incomplete))
    with pytest.raises(RuntimeError, match="incomplete result"):
        osm.fetch_candidates(setup_osm, ["library"])
    assert not list(tmp_path.glob("bounded_*.json"))


def test_empty_success_is_distinct_from_provider_failure(setup_osm, monkeypatch):
    monkeypatch.setattr(overpass, "cancellable_request", lambda *a, **k: provider_response({"elements": []}))
    candidates, error = osm.fetch_candidates(setup_osm, ["library"])
    assert candidates is None and error.kind == "not_found"


def test_cancel_is_checked_before_download(setup_osm):
    def cancel():
        raise RunCancelled("cancelled")
    with pytest.raises(RunCancelled):
        osm.fetch_candidates(setup_osm, ["library"], check=cancel)


def test_subdivision_does_not_reset_remote_budget(setup_osm, monkeypatch):
    from osmnx import _overpass
    coords = _overpass._make_overpass_polygon_coord_strs(setup_osm.geometry.union_all())[0]
    monkeypatch.setattr(_overpass, "_make_overpass_polygon_coord_strs", lambda p: [coords, coords + " "])
    now, calls = [100.], []
    monkeypatch.setattr(osm.time, "monotonic", lambda: now[0])
    def fetch(*args):
        calls.append(1)
        now[0] += 100
        return payload()
    monkeypatch.setattr(osm, "_fetch_payload", fetch)
    with pytest.raises(RuntimeError, match="100-second budget"):
        osm.fetch_candidates(setup_osm, ["library"])
    assert len(calls) == 1


def test_planner_provider_failure_is_bounded_and_never_reported_as_empty(setup_osm, monkeypatch):
    calls = []
    def post(method, url, **kwargs):
        calls.append(url)
        raise httpx.ReadTimeout("stalled provider")
    monkeypatch.setattr(overpass, "cancellable_request", post)
    with pytest.raises(RuntimeError, match="lookup failed"):
        osm.fetch_candidates(setup_osm, ["library"])
    assert len(calls) == 2
    with pytest.raises(RuntimeError, match="same OpenStreetMap lookup"):
        osm.fetch_candidates(setup_osm, ["library"])
    assert len(calls) == 2


def test_compact_query_preserves_exact_union_and_full_descendant_geometry():
    coords = "1 2 3 4 1 2"
    query = osm._bounded_features_query(coords, {"amenity": ["school", "library"], "leisure": ["sports_centre"]})
    assert query.count(coords) == 2
    assert 'nwr["amenity"~"^(library|school)$"]' in query
    assert 'nwr["leisure"~"^(sports_centre)$"]' in query
    assert query.endswith(";(._;>;);out;")
    assert "out center" not in query and "out skel" not in query


def test_hosted_city_query_has_execution_headroom_without_changing_geometry(setup_osm, monkeypatch):
    monkeypatch.setenv("OASIS_OVERPASS_URL", "https://api.overpass.ch/v1/interpreter")
    monkeypatch.setenv("OVERPASS_CH_API_KEY", "test")
    calls = []
    def post(method, url, **kwargs):
        calls.append(kwargs)
        return provider_response(payload())
    monkeypatch.setattr(overpass, "cancellable_request", post)
    candidates, error = osm.fetch_candidates(setup_osm, ["library"])
    assert error is None and len(candidates) == 1
    assert "[timeout:45]" in calls[0]["data"]["data"]
    assert "[maxsize:134217728]" in calls[0]["data"]["data"]
    assert "poly:" not in calls[0]["data"]["data"]
    assert calls[0]["timeout"] == (5, 65)
    assert calls[0]["headers"]["X-API-Key"] == "test"
    assert candidates.iloc[0]["capacity_observed_people"] == 100


def test_hosted_bbox_prefilter_keeps_the_exact_study_boundary(setup_osm, monkeypatch):
    boundary = gpd.GeoDataFrame(geometry=[Polygon([(-117.38, 33.98), (-117.37, 33.98), (-117.38, 33.99)])], crs=4326)
    rows = {"elements": [
        {"type": "node", "id": 1, "lon": -117.378, "lat": 33.982, "tags": {"amenity": "library", "name": "Inside"}},
        {"type": "node", "id": 2, "lon": -117.371, "lat": 33.989, "tags": {"amenity": "library", "name": "Outside polygon, inside bbox"}},
    ]}
    monkeypatch.setenv("OASIS_OVERPASS_URL", "https://api.overpass.ch/v1/interpreter")
    monkeypatch.setattr(overpass, "cancellable_request", lambda *a, **k: provider_response(rows))
    candidates, error = osm.fetch_candidates(boundary, ["library"])
    assert error is None and list(candidates["name"]) == ["Inside"]
