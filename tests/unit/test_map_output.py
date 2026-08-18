"""Offline tests for MapLibre GeoJSON, CRS, and scoped map output behavior."""
import json

import geopandas as gpd
from shapely.geometry import Point, box

from oasis.contracts.domain import SiteSolution, TaskSpec
from oasis.core.outputs.maps import comparison_map, coverage_map, heat_exposure_map


def _layers(path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    grouped = {}
    for feature in payload["features"]:
        grouped.setdefault(feature["properties"]["layer_kind"], []).append(feature)
    return payload, grouped


def test_coverage_map_reprojects_and_writes_maplibre_layers(tmp_path):
    """Projected inputs become aligned longitude/latitude features with typed properties."""
    tracts = gpd.GeoDataFrame(
        {"GEOID": ["36061000100"], "total_population": [1000], "elderly_ratio": [.2],
         "poverty_rate": [.1], "heat_exposure": [30.], "mean_lst": [31.],
         "population_weighted_lst": [30.], "exposure_gap": [-1.],
         "valid_lst_population_share": [.95], "heat_exposure_method": ["population_weighted_grid"]},
        geometry=[box(-74.02, 40.70, -73.98, 40.74)], crs="EPSG:4326",
    ).to_crs(3857)
    candidates = gpd.GeoDataFrame(
        {"cand_id": ["a"], "name": ["New York Public Library"]},
        geometry=[Point(-74.0, 40.72)], crs="EPSG:4326",
    ).to_crs(3857)
    boundary = gpd.GeoDataFrame(geometry=[box(-74.03, 40.69, -73.97, 40.75)], crs="EPSG:4326")
    solution = SiteSolution(
        profile_name="test", selected_ids=["a"], covered_population=1,
        covered_ratio=1, vulnerable_covered_ratio=1, mean_travel_minutes=1,
        weights_used=None, solver_status="Optimal",
    )
    path = tmp_path / "map.geojson"

    coverage_map(tracts, candidates, solution, "heat_exposure", str(path), boundary)

    payload, layers = _layers(path)
    assert payload["metadata"]["renderer"] == "maplibre"
    assert payload["metadata"]["indicator_label"] == "Heat exposure (°C)"
    assert set(layers) == {"tract", "boundary", "selected_site"}
    assert layers["tract"][0]["properties"]["population"] == 1000
    assert layers["tract"][0]["properties"]["population_weighted_heat_c"] == 30.0
    assert layers["tract"][0]["properties"]["tract_mean_heat_c"] == 31.0
    assert layers["tract"][0]["properties"]["heat_exposure_method"] == "Population weighted using the 100 m population grid"
    assert layers["selected_site"][0]["properties"]["name"] == "New York Public Library"
    assert abs(layers["boundary"][0]["geometry"]["coordinates"][0][0][0] + 73.97) < 1e-6
    assert "-8230000" not in path.read_text(encoding="utf-8")


def test_heat_exposure_map_writes_tracts_and_boundary(tmp_path):
    """Heat-only output is a self-describing MapLibre FeatureCollection."""
    tracts = gpd.GeoDataFrame(
        {"GEOID": ["1", "2"], "total_population": [100, 200],
         "elderly_ratio": [.1, .2], "poverty_rate": [.2, .3], "heat_exposure": [39., 42.]},
        geometry=[box(-112.10, 33.42, -112.05, 33.47), box(-112.05, 33.42, -112.00, 33.47)],
        crs="EPSG:4326",
    )
    boundary = gpd.GeoDataFrame(geometry=[box(-112.11, 33.41, -111.99, 33.48)], crs="EPSG:4326")
    path = tmp_path / "heat.geojson"

    heat_exposure_map(tracts, str(path), boundary)

    payload, layers = _layers(path)
    assert payload["metadata"]["value_range"] == [39.0, 42.0]
    assert [feature["properties"]["heat_c"] for feature in layers["tract"]] == [39.0, 42.0]
    assert len(layers["boundary"]) == 1


def test_comparison_map_writes_two_interactive_marker_roles(tmp_path):
    """Baseline and alternative selections remain distinct, including shared sites."""
    tracts = gpd.GeoDataFrame(
        {"GEOID": ["1"], "heat_exposure": [40.0]},
        geometry=[box(-112.1, 33.4, -112.0, 33.5)], crs="EPSG:4326",
    )
    candidates = gpd.GeoDataFrame(
        {"cand_id": ["a", "b", "shared"], "name": ["A", "B", "Shared"]},
        geometry=[Point(-112.08, 33.44), Point(-112.02, 33.46), Point(-112.05, 33.45)],
        crs="EPSG:4326",
    )
    path = tmp_path / "comparison.geojson"
    comparison_map(
        tracts, candidates,
        {"baseline": ("Balanced", ["a", "shared"]),
         "alternative": ("Equity first", ["b", "shared"])},
        str(path),
    )

    _, layers = _layers(path)
    markers = layers["comparison_site"]
    assert len(markers) == 4
    assert {item["properties"]["comparison_role"] for item in markers} == {"baseline", "alternative"}
    assert sum(item["properties"]["candidate_id"] == "shared" for item in markers) == 2


def test_coverage_map_exposes_selected_site_hours_as_typed_fields(tmp_path):
    """Selected-site features expose operating evidence without embedded popup HTML."""
    tracts = gpd.GeoDataFrame(
        {"GEOID": ["tract-1"], "heat_exposure": [40.0]},
        geometry=[box(-97.76, 30.25, -97.74, 30.27)], crs="EPSG:4326",
    )
    candidates = gpd.GeoDataFrame(
        {"cand_id": ["site-1"], "name": ["Central Cooling Center"],
         "opening_hours": ["Mo-Fr 09:00-19:00"], "hours_status": ["open"]},
        geometry=[Point(-97.73, 30.26)], crs="EPSG:4326",
    )
    solution = SiteSolution(
        profile_name="test", selected_ids=["site-1"], covered_population=100,
        covered_ratio=1, vulnerable_covered_ratio=1, mean_travel_minutes=7.5,
        weights_used=None, solver_status="Optimal",
    )
    task = TaskSpec(
        place_name="Austin", mobility_scenario="driving",
        operating_window={"date": "2026-07-29", "start": "14:00", "end": "18:00", "unknown_policy": "include"},
    )
    path = tmp_path / "mobility-hours.geojson"

    coverage_map(tracts, candidates, solution, "heat_exposure", str(path), task=task)

    _, layers = _layers(path)
    site = layers["selected_site"][0]["properties"]
    assert site["opening_hours"] == "Mo-Fr 09:00-19:00"
    assert site["hours_label"] == "Open for the full requested window"
    assert site["requested_window"] == "2026-07-29 14:00-18:00"
    assert site["mobility"] == "Driving"
