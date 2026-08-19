"""Offline tests for representative route helpers."""
import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import LineString, Point, box

from oasis.core.spatial.routes import _directions_url, _length_weighted_environment, plan_point_to_nearest_site


def test_matrix_url_converts_to_directions_geojson():
    assert _directions_url(
        "https://api.heigit.org/openrouteservice/v2/matrix", "foot-walking"
    ) == "https://api.heigit.org/openrouteservice/v2/directions/foot-walking/geojson"


def test_route_environment_is_weighted_by_intersection_length():
    areas = gpd.GeoDataFrame(
        {"mean_lst": [30.0, 40.0], "mean_ndvi": [.6, .2],
         "mean_ndbi": [-.1, .3], "mean_albedo": [.2, .4]},
        geometry=[box(0, 0, 1, 1), box(1, 0, 3, 1)], crs="EPSG:3857",
    ).to_crs(4326)
    line = gpd.GeoSeries([LineString([(0, .5), (3, .5)])], crs=3857).to_crs(4326).iloc[0]
    result = _length_weighted_environment(line, areas)
    assert np.isclose(result["route_mean_lst_c"], (30 + 2 * 40) / 3, atol=.01)
    assert np.isclose(result["route_mean_ndvi"], (.6 + 2 * .2) / 3, atol=.01)


def test_point_route_rejects_origin_outside_tract_before_network(monkeypatch):
    areas = gpd.GeoDataFrame(
        {"GEOID": ["1"], "mean_lst": [35.0]},
        geometry=[box(-112.1, 33.4, -112.0, 33.5)], crs="EPSG:4326",
    )
    sites = gpd.GeoDataFrame(
        {"cand_id": ["site"]}, geometry=[Point(-112.05, 33.45)], crs="EPSG:4326",
    )
    monkeypatch.setenv("ORS_API_KEY", "unused")
    with pytest.raises(ValueError, match="outside the current study area"):
        plan_point_to_nearest_site(
            -120, 40, areas, sites, ["site"], mode="walking"
        )


def test_point_route_accepts_origin_inside_boundary_but_outside_retained_tract(monkeypatch):
    areas = gpd.GeoDataFrame(
        {"GEOID": ["1"], "mean_lst": [35.0]},
        geometry=[box(-112.1, 33.4, -112.0, 33.5)], crs="EPSG:4326",
    )
    sites = gpd.GeoDataFrame(
        {"cand_id": ["site"], "name": ["Center"]},
        geometry=[Point(-112.05, 33.45)], crs="EPSG:4326",
    )
    monkeypatch.setenv("ORS_API_KEY", "test")
    monkeypatch.setattr("oasis.core.spatial.routes.post_ors", lambda *args, **kwargs: {"durations": [[600.0]]})
    monkeypatch.setattr("oasis.core.spatial.routes._cached_route", lambda *args, **kwargs: ({
        "type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-112.15, 33.45], [-112.05, 33.45]]},
        "properties": {"summary": {"duration": 600, "distance": 9000}},
    }, "live"))

    result = plan_point_to_nearest_site(
        -112.15, 33.45, areas, sites, ["site"], mode="walking",
        study_boundary=box(-112.2, 33.3, -111.9, 33.6),
    )

    assert result["summary"]["origin_geoid"] is None
    assert result["summary"]["destination_id"] == "site"


def test_active_heat_audit_is_explicitly_post_route(monkeypatch):
    areas = gpd.GeoDataFrame({"GEOID": ["1"], "mean_lst": [35.0]},
                             geometry=[box(-112.1, 33.4, -112.0, 33.5)], crs="EPSG:4326")
    sites = gpd.GeoDataFrame({"cand_id": ["site"], "name": ["Center"]},
                             geometry=[Point(-112.02, 33.48)], crs="EPSG:4326")
    monkeypatch.setenv("ORS_API_KEY", "test")
    monkeypatch.setattr("oasis.core.spatial.routes.post_ors", lambda *args, **kwargs: {"durations": [[600.0]]})
    monkeypatch.setattr("oasis.core.spatial.routes._cached_route", lambda *args, **kwargs: ({
        "type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-112.05, 33.45], [-112.02, 33.48]]},
        "properties": {"summary": {"duration": 600, "distance": 2000}},
    }, "live"))
    result = plan_point_to_nearest_site(-112.05, 33.45, areas, sites, ["site"], mode="walking")
    audit = result["summary"]["active_heat_audit"]
    assert audit["status"] == "available"
    assert audit["exposure_celsius_minutes"] == result["summary"]["surface_temperature_minutes"]
    assert "does not alter" in audit["interpretation"]


def test_point_route_shortlists_before_calling_ors(monkeypatch):
    areas = gpd.GeoDataFrame(
        {"GEOID": ["1"], "mean_lst": [35.0]},
        geometry=[box(-113, 33, -111, 35)], crs="EPSG:4326",
    )
    sites = gpd.GeoDataFrame(
        {"cand_id": [f"site-{index}" for index in range(12)]},
        geometry=[Point(-112 + index * .01, 34) for index in range(12)],
        crs="EPSG:4326",
    )
    captured = {}
    monkeypatch.setenv("ORS_API_KEY", "test")
    def fake_matrix(*args, **kwargs):
        captured["locations"] = kwargs.get("payload", {}).get("locations")
        if captured["locations"] is None:
            captured["locations"] = args[2]["locations"]
        return {"durations": [[600.0] * 5]}
    monkeypatch.setattr("oasis.core.spatial.routes.post_ors", fake_matrix)
    monkeypatch.setattr("oasis.core.spatial.routes._cached_route", lambda *args, **kwargs: ({
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [[-112, 34], [-111.99, 34]]},
        "properties": {"summary": {"duration": 600, "distance": 1000}},
    }, "live"))
    result = plan_point_to_nearest_site(
        -112, 34, areas, sites, sites["cand_id"].tolist(),
        mode="walking", candidate_limit=5,
    )
    assert len(captured["locations"]) == 6
    assert result["summary"]["selected_facility_pool"] == 12
    assert result["summary"]["ors_shortlist_size"] == 5
