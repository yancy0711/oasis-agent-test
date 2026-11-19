"""Regional source, confirmed upload, and solver regression coverage."""
from pathlib import Path
from threading import Lock
import json

import geopandas as gpd
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import box

from api.main import create_app
from oasis.agent.context import Context
from oasis.contracts.domain import Provenance, TaskSpec
from oasis.core.data import regional, shenzhen
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
        {"stpug": ["A", "B"], "t_pop": [900, 701], "age_5": [231, 131]},
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
    census_data = client.get("/v1/regional-data", params={"place_name": place}).json()
    assert census_data["year"] == 2021 and census_data["units"] == census_data["older_units"] == 1
    assert len(census_data["geometry"]["features"]) == 1
    downloaded = client.get("/v1/regional-data/download", params={"place_name": place}).json()
    assert downloaded["features"][0]["properties"]["elderly_population"] == 231
    context = Context()
    result = workflow.resolve_study_area(context, place)
    assert result.ok, result.error
    assert context.get("study")["regional_demand"]["total_population"].tolist() == [900]


def test_confirmed_geojson_and_candidate_csv_reach_the_solver(monkeypatch, tmp_path):
    monkeypatch.setattr(regional, "UPLOAD_ROOT", tmp_path)
    monkeypatch.setattr(regional, "automatic_data", lambda *_: pytest.fail("A confirmed upload takes precedence"))
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
