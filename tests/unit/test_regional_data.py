"""Regional source, confirmed upload, and solver regression coverage."""
from pathlib import Path
from threading import Lock
import json

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import rasterio
from fastapi.testclient import TestClient
from rasterio.transform import from_origin
from shapely.geometry import box

from api.main import create_app
from oasis.agent.context import Context
from oasis.contracts.domain import Provenance, TaskSpec, ValueWeights
from oasis.core.data import grdi, regional, shenzhen
from oasis.core.spatial.coordinates import gcj02_to_wgs84
from oasis.services import workflow
from oasis.services.application import OasisService


EXAMPLES = Path(__file__).resolve().parents[2] / "docs" / "examples"
FIELDS = {"id": "分區編號", "population": "總人口", "older": "65歲及以上人口"}


def regional_service(tmp_path):
    service = object.__new__(OasisService)
    service.cfg = {}
    service.lock = Lock()
    service.resolutions = {}
    service.upload_root = tmp_path
    service.uploads = {}
    return service


def test_named_hong_kong_district_uses_approved_census_adapter(monkeypatch, tmp_path):
    districts = gpd.GeoDataFrame({"dc_eng": ["Kowloon City"]}, geometry=[box(114.17, 22.30, 114.23, 22.35)], crs=4326)
    census = gpd.GeoDataFrame(
        {"stpug": ["A", "B"], "t_pop": [900, 701], "age_5": [231, 131],
         "dh": [300, 0], "dhi_1": [15, 10], "dhi_2": [45, 0]},
        geometry=[box(114.18, 22.31, 114.19, 22.32), box(114.14, 22.31, 114.15, 22.32)], crs=4326,
    )
    layers = {regional._DISTRICT_LAYER: districts, regional._DEMAND_LAYER: census}
    monkeypatch.setattr(regional, "_hk_layer", lambda layer, *_: layers[layer].copy())
    client = TestClient(create_app(regional_service(tmp_path)))
    resolved = client.post("/v1/places/resolve", json={"place_name": "九龍城區"}).json()
    confirmed = client.post("/v1/places/confirm", json={
        "resolution_id": resolved["resolution_id"], "candidate_id": resolved["selected"]["candidate_id"],
    }).json()
    place = confirmed["canonical_name"]
    assert place == "Kowloon City, Hong Kong"
    assert confirmed["socioeconomic_indicator"] == regional.HK_INCOME_FIELD
    census_data = client.get("/v1/regional-data", params={"place_name": place}).json()
    assert census_data["year"] == 2021 and census_data["units"] == census_data["older_units"] == 1
    assert len(census_data["geometry"]["features"]) == 1
    assert census_data["socioeconomic"]["observed_zones"] == 1
    assert census_data["geometry"]["features"][0]["properties"]["low_income_household_pct"] == 20
    downloaded = client.get("/v1/regional-data/download", params={"place_name": place}).json()
    assert downloaded["features"][0]["properties"]["elderly_population"] == 231
    assert downloaded["features"][0]["properties"][regional.HK_INCOME_FIELD] == .2
    context = Context()
    result = workflow.resolve_study_area(context, place)
    assert result.ok, result.error
    assert context.get("study")["regional_demand"]["total_population"].tolist() == [900]
    _, all_units = regional.hong_kong_data("Hong Kong")
    assert pd.isna(all_units[regional.HK_INCOME_FIELD].iloc[1])
    changed = all_units.copy()
    changed.geometry = changed.geometry.translate(xoff=.001)
    assert np.isnan(regional.matching_hong_kong_income(place, changed)).all()
    assert regional.matching_hong_kong_income(place, all_units)[0] == .2


def test_confirmed_geojson_and_candidate_csv_reach_the_solver(monkeypatch, tmp_path):
    monkeypatch.setattr(regional, "UPLOAD_ROOT", tmp_path)
    monkeypatch.setattr(regional, "automatic_data", lambda *_: pytest.fail("A confirmed upload takes precedence"))
    monkeypatch.setattr(regional, "matching_hong_kong_income", lambda _, areas: np.full(len(areas), np.nan))
    client = TestClient(create_app(regional_service(tmp_path)))
    filename = "hong-kong-demand.geojson"
    request_data = {"place_name": "Kowloon City, Hong Kong", "id_field": FIELDS["id"],
                    "population_field": FIELDS["population"], "older_field": FIELDS["older"], "year": "2024"}
    response = client.post("/v1/uploads/demand", data=request_data, files={
        "file": (filename, (EXAMPLES / filename).read_bytes(), "application/geo+json"),
    })
    assert response.status_code == 200, response.text
    upload = response.json()
    invalid = client.post("/v1/uploads/demand", data={**request_data, "epsg": "2326"}, files={
        "file": (filename, (EXAMPLES / filename).read_bytes(), "application/geo+json"),
    })
    assert invalid.status_code == 422

    task = TaskSpec(place_name=upload["place_name"], demand_upload_id=upload["upload_id"],
                    heat_metric="landsat_lst", n_facilities=1, threshold_minutes=5,
                    candidate_source="csv")
    context = Context()
    context.put("task", task)
    context.put("config", {"optimization": {"pareto_workers": 1}})
    monkeypatch.setattr(workflow, "fetch_heat_exposure", lambda areas, *args, **kwargs: (
        pd.DataFrame({"GEOID": areas["GEOID"], "mean_lst": [35 - .5 * i for i in range(len(areas))]}),
        Provenance(source="Offline synthetic Landsat fixture"),
    ))
    for result in (
        workflow.resolve_study_area(context, task.place_name),
        workflow.prepare_demand(context),
        workflow.load_candidate_sites(context, str(EXAMPLES / "hong-kong-candidates.csv")),
        workflow.build_travel_matrix(context),
        workflow.optimize_sites(context, n_facilities=1, threshold_minutes=5),
        workflow.generate_outputs(context, str(tmp_path / "run")),
    ):
        assert result.ok, result.error
    demand = context.get("demand")
    assert demand["population"].tolist() == [1200, 700, 320, 460, 520, 290]
    assert demand["group_population"]["older_adults"].tolist() == [370, 80, 66, 95, 155, 55]
    assert context.get("analysis")["solution"].selected_ids == ["demo-west"]
    assert {"poverty_rate", "no_vehicle_ratio"} <= set(context.get("quality")["regional_data"]["missing"])
    outputs = context.get("outputs")
    artifact = json.loads(Path(outputs["map"]).read_text(encoding="utf-8"))
    assert artifact["features"]
    assert filename in Path(outputs["report"]).read_text(encoding="utf-8")


def test_mainland_grdi_partial_observations_reach_the_siting_solver(monkeypatch, tmp_path):
    path = tmp_path / "grdi.tif"
    monkeypatch.setenv("OASIS_GRDI_PATH", str(path))
    with rasterio.open(
        path, "w", driver="GTiff", count=1, width=4, height=1, dtype="float32",
        crs=4326, transform=from_origin(114, 23, .01, .01), nodata=-9999,
    ) as raster:
        raster.write(np.array([[10, 50, 90, -9999]], dtype=np.float32), 1)
    areas = gpd.GeoDataFrame(
        {"GEOID": ["left", "middle", "right"], "total_population": [100] * 3,
         "elderly_population": [30, 20, 10]},
        geometry=[box(114, 22.99, 114.015, 23), box(114.02, 22.99, 114.03, 23),
                  box(114.031, 22.99, 114.039, 23)], crs=4326,
    )
    weights = ValueWeights(population_density=0, elderly_ratio=0, poverty_rate=1, heat_exposure=0)
    task = TaskSpec(place_name=shenzhen.NAME, heat_metric="landsat_lst", weights=weights, n_facilities=1)
    context = Context()
    context.put("task", task)
    context.put("config", {"optimization": {"pareto_workers": 1}})
    context.put("study", {"place_name": shenzhen.NAME, "year": 2022, "regional_demand": areas,
                          "cache_scope": "regional-test", "boundary": areas[["geometry"]].dissolve(),
                          "regional_provenance": Provenance(source="Synthetic population")})
    monkeypatch.setattr(workflow, "utci_paths", lambda *_: [])
    monkeypatch.setattr(workflow, "fetch_heat_exposure", lambda zones, *args, **kwargs: (
        pd.DataFrame({"GEOID": zones["GEOID"], "mean_lst": [35.0] * 3}), Provenance(source="Synthetic LST"),
    ))
    prepared = workflow.prepare_demand(context)
    assert prepared.ok, prepared.error
    demand = context.get("demand")
    observed = demand["observed_indicators"][grdi.FIELD]
    assert observed.tolist()[:2] == pytest.approx([70 / 3, 90])
    assert pd.isna(observed.iloc[2]) and pd.isna(demand["areas"][grdi.FIELD].iloc[2])
    assert demand["indicators"]["poverty_rate"].tolist() == pytest.approx([70 / 3, 90, 170 / 3])
    assert not demand["group_population"]["below_poverty"].any()
    context.put("candidates", gpd.GeoDataFrame({"cand_id": ["left-site", "middle-site", "right-site"]},
                                               geometry=demand["points"].geometry, crs=demand["points"].crs))
    context.put("travel", {"matrix": np.array([[0, 30, 30], [30, 0, 30], [30, 30, 0]], dtype=float)})
    result = workflow.optimize_sites(context, n_facilities=1, threshold_minutes=15, weights=weights.model_dump())
    assert result.ok, result.error
    assert result.summary["selected_ids"] == ["middle-site"]
    assert result.summary["applied_weights"] == {grdi.FIELD: 1.0}
    assert result.summary["socioeconomic"]["objective_median_filled_zones"] == 1
    report = workflow.generate_outputs(context, str(tmp_path / "run"))
    assert report.ok, report.error
    content = Path(report.summary["report"]).read_text(encoding="utf-8")
    assert "deprivation_index=1.000" in content and "poverty_rate=" not in content
    assert "GRDI (0–100) · 2/3 demand zones observed." in content
    assert "median observed value in 1 zone." in content


def test_international_boundary_uses_worldpop_without_a_city_registry(monkeypatch, tmp_path):
    query = "Gulou District, Nanjing, China"
    canonical = "Gulou District, Nanjing City, Jiangsu, China"
    polygon = box(118.72, 32.04, 118.78, 32.11)
    row = {"display_name": canonical, "geojson": polygon.__geo_interface__, "osm_type": "relation",
           "osm_id": 2139790, "address": {"country_code": "cn"}, "addresstype": "city"}
    monkeypatch.setattr(regional, "CACHE_DIR", tmp_path)
    monkeypatch.setattr("oasis.services.application.select_place_candidate",
                        lambda name: pytest.fail(f"Explicit China query used the US geocoder: {name}"))
    monkeypatch.setattr(regional, "international_boundary_candidates", lambda name: [row] if name == query else [])
    calls = []

    def population(batch):
        calls.append(len(batch))
        assert batch.geometry.representative_point().covered_by(polygon).all()
        return [100] * len(batch), [18] * len(batch)

    monkeypatch.setattr(shenzhen, "_population", population)
    client = TestClient(create_app(regional_service(tmp_path)))
    resolved = client.post("/v1/places/resolve", json={"place_name": query}).json()
    assert resolved["preview_boundary"]["features"][0]["geometry"]["type"] == "Polygon"
    assert regional.regional_place(canonical) is None
    confirmed = client.post("/v1/places/confirm", json={
        "resolution_id": resolved["resolution_id"], "candidate_id": resolved["selected"]["candidate_id"],
    }).json()
    assert confirmed["canonical_name"] == canonical
    assert client.get("/v1/regional-data/download", params={"place_name": canonical, "layer": "boundary"}).is_success
    assert not calls
    data = client.get("/v1/regional-data", params={"place_name": canonical}).json()
    assert data["units"] == data["older_units"] == sum(calls)
    assert len(calls) >= 2 and max(calls) <= 20
    cached_calls = calls.copy()
    context = Context()
    result = workflow.resolve_study_area(context, canonical)
    assert result.ok, result.error
    assert context.get("study")["regional_timezone"] == "Asia/Shanghai"
    assert calls == cached_calls


def test_nanshan_uses_batch_estimates_and_exports_demand_and_boundary(monkeypatch, tmp_path):
    district = box(113.91, 22.52, 113.97, 22.54)
    monkeypatch.setattr(shenzhen, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(shenzhen, "_boundary", lambda: district)

    def batch(url, *, json, timeout):
        assert url.endswith("/v3/batch") and json["include"] == "agesex"
        assert json["year"] == 2022
        rows = [
            {"id": feature["id"], "total_population": 100,
             "agesex_pyramid": [{"class": 20, "female": 47, "male": 47}] +
             [{"class": age, "female": 1, "male": 0} for age in range(65, 91, 5)]}
            for feature in reversed(json["features"]["features"])
        ]
        return type("BatchResponse", (), {
            "raise_for_status": lambda self: None,
            "json": lambda self: {"data_year": 2022, "features": rows},
        })()

    monkeypatch.setattr(shenzhen.requests, "post", batch)
    client = TestClient(create_app(regional_service(tmp_path)))
    resolved = client.post("/v1/places/resolve", json={"place_name": "深圳南山區"}).json()
    selected = resolved["selected"]
    assert selected["candidate_id"].startswith("sz:")
    assert selected["canonical_name"] == shenzhen.NAME
    params = {"place_name": shenzhen.NAME}
    # The boundary is downloadable independently, before WorldPop runs.
    extent = client.get("/v1/regional-data/download", params={**params, "layer": "boundary"}).json()
    assert extent["metadata"]["source"] == "OpenStreetMap"
    assert len(extent["features"]) == 1
    result = client.get("/v1/regional-data", params=params).json()
    assert result["source"] == shenzhen.SOURCE and result["units"] > 1
    assert result["older_units"] == result["units"]
    export = client.get("/v1/regional-data/download", params=params).json()
    assert len(export["features"]) == result["units"]
    assert export["features"][0]["properties"]["elderly_population"] == 6


def test_explicit_gcj02_and_epsg_uploads_align_in_wgs84(tmp_path):
    from pyproj import Transformer
    from shapely.geometry import Point, shape

    longitude, latitude = 116.403372494, 39.917930749
    wgs84 = tuple(float(v) for v in gcj02_to_wgs84(longitude, latitude))
    assert wgs84 == pytest.approx((116.397128, 39.916527), abs=.00001)

    client = TestClient(create_app(regional_service(tmp_path)))
    feature = {"type": "Feature", "properties": {"id": "one", "pop": 50},
               "geometry": box(longitude - .001, latitude - .001, longitude + .001, latitude + .001).__geo_interface__}
    demand = client.post("/v1/uploads/demand", data={"place_name": "Beijing demo", "id_field": "id",
                         "population_field": "pop", "coordinate_system": "gcj02"},
                         files={"file": ("areas.geojson", json.dumps({"type": "FeatureCollection", "features": [feature]}))})
    assert demand.status_code == 200, demand.text
    restored = shape(demand.json()["geometry"]["features"][0]["geometry"])
    assert restored.covers(Point(*wgs84))

    for values in (
        (longitude, latitude, {"coordinate_system": "gcj02"}),
        (*Transformer.from_crs(4326, 3857, always_xy=True).transform(*wgs84), {"epsg": "3857"}),
    ):
        x, y, params = values
        response = client.post("/v1/uploads", data=params, files={
            "file": ("sites.csv", f"name,longitude,latitude\nExample,{x},{y}\n"),
        })
        assert response.status_code == 200, response.text
        lon, lat, _ = response.json()["preview_points"][0]
        assert (lon, lat) == pytest.approx(wgs84, abs=1e-8)
        assert restored.covers(Point(lon, lat))
