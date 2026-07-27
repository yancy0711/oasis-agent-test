"""Offline tests for place resolution, scoped caching, and routing gates."""
import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import Point

from agent.memory import Context
from agent.schemas import TaskSpec
from hitl import elicitation
from robustness import cache
from tools import workflow
from tools.data_acquisition import geocoding
from tools.registry import registry


def _candidate(name: str, kind: str, state: str = "California") -> dict:
    """Return one lightweight US polygon candidate for selection tests."""
    locality = name.split(",")[0]
    address = {"country": "United States", "country_code": "us", "state": state}
    if kind in geocoding.LEVEL_TYPES[8]: address["city"] = locality
    elif kind == "county": address["county"] = locality
    return {"display_name": name, "name": locality, "addresstype": kind, "address": address,
            "geojson": {"type": "Polygon", "coordinates": []}}


def test_cache_scope_separates_places(monkeypatch, tmp_path):
    """Verify canonical places are isolated and unscoped writes are rejected."""
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    with cache.scoped_cache("Los Angeles County, California, USA"): la = cache.cache_dir("heat")
    with cache.scoped_cache("San Francisco, California, USA"): sf = cache.cache_dir("heat")
    assert la != sf and "los_angeles_county_california_usa" in str(la)
    assert "san_francisco_california_usa" in str(sf)
    assert cache.place_slug("San Francisco, California, United States") == cache.place_slug("San Francisco, California, USA")
    assert cache.place_slug("LA") == "la" and cache.place_slug("\u6d1b\u6749\u77f6").startswith("place_")
    with pytest.raises(RuntimeError, match="Cache scope is not set"): cache.cache_dir("heat")


def test_usa_suffix_is_optional_and_queries_are_us_only():
    """Verify US suffix variants share a query key and Nominatim is US-only."""
    full, short = "San Francisco, California, USA", "San Francisco, California"
    assert geocoding.normalize_place_query(full) == short
    assert geocoding._candidate_filename(full) == geocoding._candidate_filename(short)
    assert geocoding._candidate_filename("NYC") == geocoding._candidate_filename("nyc")
    assert geocoding._candidate_params(full, 5)["countrycodes"] == "us"


def test_nyc_stays_city_without_canonical_state_expansion(monkeypatch):
    """Verify a unique acronym city is not expanded into a same-name state."""
    city = _candidate("New York, New York, United States", "city", "New York")
    city["osm_id"] = 175905
    calls = []

    def fake_search(query, *args):
        """Return one city and record whether an unnecessary query occurs."""
        calls.append(query); return [city]

    monkeypatch.setattr(geocoding, "search_place_candidates", fake_search)
    monkeypatch.setattr(geocoding, "cache_place_candidates", lambda *args: "test")
    selected, _ = geocoding.select_place_candidate("NYC")
    assert calls == ["NYC"] and selected["osm_id"] == 175905
    assert geocoding.canonical_place_name(selected) == "New York, New York, USA"

def test_la_requires_hitl_without_hardcoded_selection(monkeypatch):
    """Verify LA exposes city, county, and state candidates instead of guessing."""
    city = _candidate("Los Angeles, California, United States", "city")
    city["address"]["county"] = "Los Angeles County"
    county = _candidate("Los Angeles County, California, United States", "county")
    rows = [city, _candidate("Louisiana, United States", "state", "Louisiana")]

    def fake_search(query, *args):
        """Return the county only when generic metadata expansion requests it."""
        return [county] if query.startswith("Los Angeles County") else [city] if "," in query else rows

    monkeypatch.setattr(geocoding, "search_place_candidates", fake_search)
    monkeypatch.setattr(geocoding, "cache_place_candidates", lambda *args: pytest.fail("cached before HITL"))
    with pytest.raises(geocoding.AmbiguousPlaceError) as error: geocoding.select_place_candidate("LA")
    assert [row["addresstype"] for row in error.value.candidates] == ["city", "county", "state"]


def test_ambiguous_place_requires_noninteractive_confirmation(monkeypatch):
    """Verify batch mode raises before selecting an ambiguous place."""
    candidates = [_candidate("Los Angeles, California, United States", "city"),
                  _candidate("Los Angeles County, California, United States", "county")]
    error = geocoding.AmbiguousPlaceError("LA", candidates)
    monkeypatch.setattr(elicitation, "select_place_candidate", lambda *args: (_ for _ in ()).throw(error))
    with pytest.raises(geocoding.AmbiguousPlaceError): elicitation.clarify_place(TaskSpec(place_name="LA"), interactive=False)


@pytest.mark.parametrize("answer", ["1", "1)", "1\uff09"])
def test_ambiguous_place_hitl_accepts_direct_number(monkeypatch, answer):
    """Verify the first HITL prompt accepts plain or parenthesized numbers."""
    candidates = [_candidate("Los Angeles, California, United States", "city"),
                  _candidate("Los Angeles County, California, United States", "county")]
    error = geocoding.AmbiguousPlaceError("LA", candidates)
    monkeypatch.setattr(elicitation, "select_place_candidate", lambda *args: (_ for _ in ()).throw(error))
    monkeypatch.setattr(elicitation, "cache_place_candidates", lambda *args: "test")
    monkeypatch.setattr("builtins.input", lambda _: answer)
    task = elicitation.clarify_place(TaskSpec(place_name="LA"), interactive=True)
    assert task.place_name == "Los Angeles County, California, USA"
    assert "alternative selected" in task.notes


def test_short_place_reuses_canonical_cache_without_network(monkeypatch, tmp_path):
    """Verify a unique short city name reuses the full canonical cache scope."""
    row = _candidate("San Francisco, California, United States", "city")
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path); monkeypatch.setattr(geocoding, "CACHE_DIR", tmp_path)
    scope = geocoding.cache_place_candidates("San Francisco, California, USA", [row], row)
    monkeypatch.setattr(geocoding.requests, "get", lambda *args, **kwargs: pytest.fail("network used"))
    selected, _ = geocoding.select_place_candidate("San Francisco")
    assert scope == "san_francisco_california_usa"
    assert geocoding.canonical_place_name(selected) == "San Francisco, California, USA"
    assert not (tmp_path / "places" / "san_francisco").exists()


def test_county_boundary_can_reach_sparse_ors(monkeypatch):
    """Verify a county below hard limits proceeds to sparse ORS quota checks."""
    points = gpd.GeoDataFrame(geometry=[Point(-118.2, 34.0)], crs="EPSG:4326")
    candidates = gpd.GeoDataFrame({"cand_id": ["a"]}, geometry=[Point(-118.21, 34.01)], crs="EPSG:4326")
    ctx = Context(); ctx.put("config", {"accessibility": {"router": "ors", "ors_city_only": False, "ors_max_area_km2": 15000}})
    ctx.put("study", {"place_type": "county", "area_km2": 12294, "cache_scope": "test_county"})
    ctx.put("demand", {"points": points}); ctx.put("candidates", candidates); called = []

    def fake_sparse(*args, **kwargs):
        """Record entry into sparse ORS without issuing a network request."""
        called.append(True); return np.array([[7.]]), {"router": "ors_sparse", "tier": "cache"}

    monkeypatch.setattr(workflow, "sparse_network_matrix", fake_sparse)
    result = registry.get("build_travel_matrix").fn(ctx=ctx)
    assert result.ok and result.summary["router"] == "ors_sparse" and called


def test_oversized_county_reports_haversine_fallback(monkeypatch, caplog):
    """Verify a boundary above the hard limit reports its Haversine downgrade."""
    points = gpd.GeoDataFrame(geometry=[Point(-122.42, 37.77)], crs="EPSG:4326")
    candidates = gpd.GeoDataFrame({"cand_id": ["a"]}, geometry=[Point(-122.41, 37.78)], crs="EPSG:4326")
    ctx = Context(); ctx.put("config", {"accessibility": {"router": "ors", "ors_city_only": False, "ors_max_area_km2": 15000}})
    ctx.put("study", {"place_type": "county", "area_km2": 16000, "cache_scope": "large_county"})
    ctx.put("demand", {"points": points}); ctx.put("candidates", candidates)
    monkeypatch.setattr(workflow, "sparse_network_matrix", lambda *args, **kwargs: pytest.fail("ORS called"))
    result = registry.get("build_travel_matrix").fn(ctx=ctx)
    assert result.ok and result.summary["router"] == "haversine"
    assert "exceeds ORS limit" in result.summary["reason"] and "using Haversine" in caplog.text


def test_canonical_cache_does_not_overwrite_ambiguous_query(monkeypatch, tmp_path):
    """Verify canonical resolution preserves a raw query's multiple candidates."""
    city = _candidate("New York, New York, United States", "city", "New York")
    state = _candidate("New York, United States", "state", "New York")
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path); monkeypatch.setattr(geocoding, "CACHE_DIR", tmp_path)
    geocoding.cache_place_candidates("new york", [city, state], city)
    geocoding.cache_place_candidates(geocoding.canonical_place_name(city), [city], city)
    rows = geocoding._find_cached_candidates("new york", 5)
    assert rows is not None and [row["addresstype"] for row in rows] == ["city", "state"]


@pytest.mark.parametrize(("answers", "expected"), [
    ([""], "Los Angeles, California, USA"), (["y"], "Los Angeles, California, USA"),
    (["yes"], "Los Angeles, California, USA"), (["n", "1"], "Los Angeles County, California, USA"),
    (["no", "1"], "Los Angeles County, California, USA"),
])
def test_place_hitl_accepts_yes_no_forms(monkeypatch, answers, expected):
    """Verify Enter, y/yes, and n/no are accepted by place HITL."""
    candidates = [_candidate("Los Angeles, California, United States", "city"),
                  _candidate("Los Angeles County, California, United States", "county")]
    error = geocoding.AmbiguousPlaceError("LA", candidates)
    monkeypatch.setattr(elicitation, "select_place_candidate", lambda *args: (_ for _ in ()).throw(error))
    monkeypatch.setattr(elicitation, "cache_place_candidates", lambda *args: "test")
    replies = iter(answers); monkeypatch.setattr("builtins.input", lambda _: next(replies))
    assert elicitation.clarify_place(TaskSpec(place_name="LA"), interactive=True).place_name == expected


def test_threshold_over_ors_policy_uses_haversine(monkeypatch):
    """Verify a CLI-sized threshold above the ORS limit is reported and downgraded."""
    points = gpd.GeoDataFrame(geometry=[Point(-74.0, 40.7)], crs="EPSG:4326")
    candidates = gpd.GeoDataFrame({"cand_id": ["a"]}, geometry=[Point(-74.01, 40.71)], crs="EPSG:4326")
    ctx = Context(); ctx.put("config", {"accessibility": {"router": "ors", "ors_max_policy_threshold_minutes": 20}})
    ctx.put("task", TaskSpec(place_name="New York", threshold_minutes=25))
    ctx.put("study", {"place_type": "city", "area_km2": 1, "cache_scope": "new_york_new_york_usa"})
    ctx.put("demand", {"points": points}); ctx.put("candidates", candidates)
    monkeypatch.setattr(workflow, "sparse_network_matrix", lambda *args, **kwargs: pytest.fail("ORS called"))
    result = registry.get("build_travel_matrix").fn(ctx=ctx)
    assert result.ok and result.summary["router"] == "haversine"
    assert "threshold 25 minutes exceeds ORS policy limit" in result.summary["reason"]
