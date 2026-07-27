"""Offline tests for map CRS, boundary, and scoped output behavior."""
import geopandas as gpd
from shapely.geometry import Point, box

from agent.schemas import SiteSolution
from output.maps import coverage_map, heat_exposure_map


def test_coverage_map_reprojects_and_draws_study_boundary(tmp_path):
    """Verify projected inputs are rendered as aligned longitude/latitude."""
    tracts = gpd.GeoDataFrame({"GEOID": ["36061000100"], "total_population": [1000], "elderly_ratio": [.2],
                               "poverty_rate": [.1], "heat_exposure": [30.]},
                              geometry=[box(-74.02, 40.70, -73.98, 40.74)], crs="EPSG:4326").to_crs(3857)
    candidates = gpd.GeoDataFrame({"cand_id": ["a"], "name": ["New York Public Library"]}, geometry=[Point(-74.0, 40.72)], crs="EPSG:4326").to_crs(3857)
    boundary = gpd.GeoDataFrame(geometry=[box(-74.03, 40.69, -73.97, 40.75)], crs="EPSG:4326")
    solution = SiteSolution(profile_name="test", selected_ids=["a"], covered_population=1,
                            covered_ratio=1, vulnerable_covered_ratio=1, mean_travel_minutes=1,
                            weights_used=None, solver_status="Optimal")
    path = tmp_path / "map.html"
    coverage_map(tracts, candidates, solution, "heat_exposure", str(path), boundary)
    html = path.read_text(encoding="utf-8")
    assert "OSM administrative boundary (includes water)" in html and "-74.03" in html and "40.75" in html
    assert "Heat exposure (\\u00b0C)" in html and "Census tract details" in html
    assert "Census tract:" in html and "Population:" in html and "New York Public Library" in html
    assert "Selected site a" not in html and "-8230000" not in html

def test_heat_exposure_map_renders_tracts_and_boundary(tmp_path):
    """Verify heat-only analysis writes an interactive tract choropleth."""
    tracts = gpd.GeoDataFrame({"GEOID": ["1", "2"], "total_population": [100, 200],
                               "elderly_ratio": [.1, .2], "poverty_rate": [.2, .3],
                               "heat_exposure": [39., 42.]},
                              geometry=[box(-112.10, 33.42, -112.05, 33.47),
                                        box(-112.05, 33.42, -112.00, 33.47)], crs="EPSG:4326")
    boundary = gpd.GeoDataFrame(geometry=[box(-112.11, 33.41, -111.99, 33.48)], crs="EPSG:4326")
    path = tmp_path / "heat.html"
    heat_exposure_map(tracts, str(path), boundary)
    html = path.read_text(encoding="utf-8")
    assert "Heat exposure (\\u00b0C)" in html and "Census tract details" in html
    assert "OSM administrative boundary (includes water)" in html and "42.0" in html
