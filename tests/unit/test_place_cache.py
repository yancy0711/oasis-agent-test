"""Offline tests for place resolution, scoped caching, and routing gates."""
import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import Point

from oasis.agent.context import Context
from oasis.contracts.domain import TaskSpec
from cli import hitl as elicitation
from oasis.core import cache
from oasis.services import workflow
from oasis.core.data import geocoding
from oasis.services.registry import registry


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


def test_atomic_cache_publish_accepts_existing_winner_after_windows_lock(monkeypatch, tmp_path):
    target = tmp_path / "candidates.json"
    target.write_text("existing", encoding="utf-8")
    monkeypatch.setattr(cache.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(cache.os, "replace", lambda *_args: (_ for _ in ()).throw(PermissionError(5, "Access is denied")))
    cache.atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "existing"
    assert not list(tmp_path.glob(".candidates.*.json"))


def test_usa_suffix_is_optional_and_queries_are_us_only():
    """Verify US suffix variants share a query key and Nominatim is US-only."""
    full, short = "San Francisco, California, USA", "San Francisco, California"
    assert geocoding.normalize_place_query(full) == short
    assert geocoding._candidate_filename(full) == geocoding._candidate_filename(short)
    assert geocoding._candidate_filename("NYC") == geocoding._candidate_filename("nyc")
    assert geocoding._candidate_params(full, 5)["countrycodes"] == "us"


def test_route_origin_infers_smallest_supported_parent_city(monkeypatch):
    row = {
        "display_name": "Lincoln Memorial, Washington, District of Columbia, United States",
        "address": {
            "tourism": "Lincoln Memorial", "city": "Washington",
            "state": "District of Columbia", "country_code": "us",
        },
    }
    monkeypatch.setattr(geocoding, "search_origin_candidates", lambda query, limit=8: [row])
    label, place = geocoding.infer_route_study_area("Lincoln Memorial")
    assert label.startswith("Lincoln Memorial")
    assert place == "Washington, District of Columbia, USA"


def test_route_origin_cache_is_available_before_study_scope(monkeypatch, tmp_path):
    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return [{"display_name": "Papago Park"}]

    monkeypatch.setattr(geocoding, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(geocoding.requests, "get", lambda *args, **kwargs: Response())
    rows = geocoding.search_origin_candidates("Papago Park")
    assert rows == [{"display_name": "Papago Park"}]
    assert list((tmp_path / "shared" / "nominatim_route_origins").rglob("origin_*.json"))


def test_route_origin_options_expose_point_and_admin_interpretations(monkeypatch):
    """A vague name must expose plausible point and administrative meanings."""
    town = _candidate("Disney, Oklahoma, United States", "town", "Oklahoma")
    park = {
        "display_name": "Walt Disney World, Bay Lake, Florida, United States",
        "addresstype": "theme_park", "lat": "28.385", "lon": "-81.564",
        "address": {"city": "Bay Lake", "state": "Florida", "country_code": "us"},
    }
    monkeypatch.setattr(geocoding, "search_origin_candidates", lambda _query, limit=8: [town, park])
    choices, administrative = geocoding.route_origin_options("Disney")
    assert administrative is False
    assert [(choice["administrative"], choice["place_name"]) for choice in choices] == [
        (True, "Disney, Oklahoma, USA"), (False, "Bay Lake, Florida, USA"),
    ]


def test_short_administrative_route_query_becomes_study_area(monkeypatch):
    """A short administrative alias such as DC is not treated as an exact origin."""
    city = _candidate("Washington, District of Columbia, United States", "city", "District of Columbia")
    monkeypatch.setattr(geocoding, "search_origin_candidates", lambda _query, limit=8: [city])
    choices, administrative = geocoding.route_origin_options("DC")
    assert choices[0]["administrative"] is True
    assert administrative is True


def test_route_origin_reverse_geocodes_parent_for_cross_county_landmark(monkeypatch):
    """A resolved landmark without an address county remains a usable route origin."""
    park = {
        "display_name": "Yellowstone National Park, Wyoming, United States",
        "addresstype": "nature_reserve", "lat": "44.6200885", "lon": "-110.5606893",
        "address": {"state": "Wyoming", "country_code": "us"},
    }
    monkeypatch.setattr(geocoding, "search_origin_candidates", lambda _query, limit=8: [park])
    monkeypatch.setattr(
        geocoding, "reverse_origin_address",
        lambda _latitude, _longitude: {"county": "Park County", "state": "Wyoming"},
    )
    choices, administrative = geocoding.route_origin_options("Yellowstone National Park")
    assert administrative is False
    assert choices[0]["place_name"] == "Park County, Wyoming, USA"
    assert choices[0]["display_name"].startswith("Yellowstone National Park")


def test_specific_address_falls_back_through_landmark_context(monkeypatch):
    """An unrecognized POI-qualified address uses its inferred state without guessing."""
    park = {
        "display_name": "Yellowstone National Park, Wyoming, United States",
        "addresstype": "nature_reserve", "lat": "44.62", "lon": "-110.56",
        "address": {"state": "Wyoming", "country_code": "us"},
    }
    northeast = {
        "display_name": "20, Northeast Entrance Road, Park County, Wyoming, 82190, United States",
        "addresstype": "place", "lat": "45.0", "lon": "-110.7",
        "address": {"county": "Park County", "state": "Wyoming", "country_code": "us"},
    }
    south = {
        "display_name": "20, South Entrance Road, Teton County, Wyoming, United States",
        "addresstype": "place", "lat": "44.1", "lon": "-110.7",
        "address": {"county": "Teton County", "state": "Wyoming", "country_code": "us"},
    }
    responses = {
        "20 Entrance Road, Yellowstone National Park": [],
        "Yellowstone National Park": [park],
        "20 Entrance Road, Wyoming": [northeast, south],
    }
    monkeypatch.setattr(
        geocoding, "search_origin_candidates",
        lambda query, limit=8: responses.get(query, []),
    )
    choices, administrative = geocoding.route_origin_options(
        "20 Entrance Road, Yellowstone National Park", fallback_language=True
    )
    assert administrative is False
    assert [choice["place_name"] for choice in choices] == [
        "Park County, Wyoming, USA", "Teton County, Wyoming, USA",
    ]


@pytest.mark.parametrize(
    ("query", "expected_search"),
    [
        ("the entrance of central park in nyc", "central park, nyc"),
        ("the entrance of yellow stone park", "yellowstone park"),
    ],
)
def test_conversational_park_entrance_falls_back_to_landmark(
    monkeypatch, query, expected_search
):
    """Entrance wording returns a park representative candidate for confirmation."""
    calls = []
    park = {
        "display_name": "Matched Park, New York, United States",
        "type": "park", "addresstype": "park", "lat": "40.7", "lon": "-73.9",
        "address": {"country_code": "us", "city": "New York", "state": "New York"},
    }

    def fake_search(value, limit=8):
        calls.append(value.casefold())
        return [park] if value.casefold() == expected_search else []

    monkeypatch.setattr(geocoding, "search_origin_candidates", fake_search)
    choices, administrative = geocoding.route_origin_options(
        query, fallback_language=True
    )
    assert calls[-1] == expected_search
    assert choices[0]["display_name"] == park["display_name"]
    assert administrative is False


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("the eastern gate of Central Park in NYC", "Central Park, NYC"),
        ("D exit of Grand Central Terminal in NYC", "Grand Central Terminal, NYC"),
        ("exit D of Grand Central Terminal in NYC", "Grand Central Terminal, NYC"),
        ("at the western entrance to 350 Fifth Avenue, New York", "350 Fifth Avenue, New York"),
        ("rear access point to Phoenix Convention Center", "Phoenix Convention Center"),
    ],
)
def test_direction_and_access_labels_are_not_treated_as_landmark_names(query, expected):
    """Directional and lettered access labels are removed only from phrase starts."""
    variants = geocoding._landmark_query_variants(query)
    assert expected in variants


def test_structured_origin_search_relaxes_to_context_only_after_entity_fails(monkeypatch):
    """Deterministic search preserves hints and exposes the context as a last HITL option."""
    calls = []
    context_row = {
        "display_name": "Wyoming, United States", "type": "state",
        "addresstype": "state", "lat": "43", "lon": "-107",
        "address": {"country_code": "us", "state": "Wyoming"},
    }
    def fake_search(query, limit=8):
        calls.append(query)
        return [context_row] if query == "Wyoming" else []
    monkeypatch.setattr(geocoding, "search_origin_candidates", fake_search)
    choices, administrative = geocoding.route_origin_options(
        "Unknown landmark", "Wyoming", "eastern entrance"
    )
    assert calls == ["Unknown landmark, Wyoming", "Unknown landmark", "Wyoming"]
    assert choices[0]["positional_hint"] == "eastern entrance"
    assert administrative is True


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
    """Verify LA exposes supported city and county candidates instead of guessing."""
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
    assert [row["addresstype"] for row in error.value.candidates] == ["city", "county"]


def test_census_place_expands_to_supported_parent_boundary(monkeypatch):
    """An unsupported census result is replaced by a city or county choice."""
    census = _candidate("Port Ewen, New York, United States", "census", "New York")
    census["address"].update({"town": "Port Ewen", "county": "Ulster County"})
    county = _candidate("Ulster County, New York, United States", "county", "New York")
    calls = []

    def fake_search(query, *args):
        calls.append(query)
        return [county] if query.startswith("Ulster County") else [census]

    monkeypatch.setattr(geocoding, "search_place_candidates", fake_search)
    monkeypatch.setattr(geocoding, "cache_place_candidates", lambda *args: "test")
    selected, candidates = geocoding.select_place_candidate("Port Ewen, New York, USA")
    assert selected["addresstype"] == "county"
    assert geocoding.canonical_place_name(selected) == "Ulster County, New York, USA"
    assert all(row["addresstype"] in geocoding.LEVEL_TYPES[8] | {"county"} for row in candidates)
    assert "Ulster County, New York" in calls


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


def test_quick_routing_reports_distance_pruned_eligible_pairs():
    demand = gpd.GeoDataFrame(
        geometry=[Point(-112.07, 33.45), Point(-111.0, 34.4)], crs="EPSG:4326"
    )
    candidates = gpd.GeoDataFrame(
        {"cand_id": ["near", "far"]},
        geometry=[Point(-112.071, 33.451), Point(-110.0, 35.0)],
        crs="EPSG:4326",
    )
    ctx = Context()
    ctx.put("config", {"accessibility": {
        "router": "haversine", "mode_speeds_kmh": {"walking": 4.8},
        "ors_upper_speeds_kmh": {"walking": 6.0}, "ors_pruning_margin": 1.5,
    }})
    ctx.put("task", TaskSpec(place_name="Phoenix", threshold_minutes=15))
    ctx.put("study", {"cache_scope": "phoenix"})
    ctx.put("demand", {"points": demand})
    ctx.put("candidates", candidates)
    result = registry.get("build_travel_matrix").fn(ctx=ctx)
    report = result.summary["mode_reports"]["walking"]
    assert report["total_pairs"] == 4
    assert 0 < report["eligible_pairs"] < report["total_pairs"]
    assert report["eligible_pairs"] + report["pruned_pairs"] == report["total_pairs"]


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
