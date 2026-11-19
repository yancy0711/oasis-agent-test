"""Offline ERA5-HEAT and thermal run regression tests."""

import json

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import xarray as xr
from shapely.geometry import box

from oasis.agent.context import Context
from oasis.contracts.domain import Provenance, SiteSolution, TaskSpec
from oasis.core.data.thermal import load_utci
from oasis.core.data.era5_download import download_utci, regional_boxes
from oasis.core.outputs.maps import heat_exposure_map
from oasis.core.outputs.report import render_report
from oasis.services import workflow


@pytest.fixture
def sf_tracts():
    return gpd.GeoDataFrame(
        {"GEOID": ["06075000100", "06075000200", "06075000300"], "ALAND": [100_000] * 3},
        geometry=[box(-122.48, 37.74, -122.47, 37.75),
                  box(-122.42, 37.76, -122.41, 37.77),
                  box(-122.35, 37.78, -122.34, 37.79)],
        crs=4326,
    )


@pytest.fixture
def sample_file(tmp_path):
    # Pacific days start at 07:00 UTC in July. The leading hour must be excluded.
    times = pd.date_range("2022-07-01 06:00", periods=50, freq="h")
    values = np.full((50, 1, 2), 20.0)
    values[0, 0] = [60, 60]
    values[24, 0] = [32, 20]  # 23:00 Pacific, July 1
    values[25, 0] = [28, 20]  # 00:00 Pacific, July 2
    values[27, 0] = [25, 24]
    values[49, 0] = [60, 60]
    dataset = xr.Dataset(
        {"utci": (("valid_time", "latitude", "longitude"), values + 273.15, {"units": "K"})},
        coords={"valid_time": times, "latitude": [37.75], "longitude": [237.5, 237.75]},
    )
    path = tmp_path / "sample.nc"
    dataset.to_netcdf(path, engine="netcdf4")
    return path


@pytest.mark.parametrize("cds_names", [False, True])
def test_sf_utci_uses_local_days_and_native_cells(sample_file, sf_tracts, tmp_path, cds_names):
    path = sample_file
    if cds_names:
        path = tmp_path / "cds-subset.nc"
        with xr.open_dataset(sample_file) as dataset:
            subset = dataset.rename({"valid_time": "time", "latitude": "lat", "longitude": "lon"})
            subset["utci"].attrs.pop("units")
            subset.to_netcdf(path)
        with pytest.raises(ValueError, match="must identify its UTCI unit"):
            load_utci([path], sf_tracts, "2022-07-01", "2022-07-02", timezone="America/Los_Angeles")
    options = {"timezone": "America/Los_Angeles", "cds_download": cds_names}
    values, cells = load_utci([path], sf_tracts, "2022-07-01", "2022-07-02", **options)
    np.testing.assert_allclose(values, [30, 30, 22])
    assert cells == 2
    with pytest.raises(ValueError, match="incomplete"):
        load_utci([path], sf_tracts, "2022-07-01", "2022-07-03", **options)


@pytest.mark.parametrize("place,heat_metric,auto", [
    ("San Francisco", "landsat_lst", False), ("San Francisco", "utci", False),
    ("New York", "landsat_lst", False), ("New York", "utci", False), ("New York", "utci", True),
])
def test_regional_heat_selection_controls_demand_map_and_method(
    monkeypatch, tmp_path, sf_tracts, sample_file, place, heat_metric, auto,
):
    if place == "New York":
        sf_tracts.geometry = sf_tracts.translate(xoff=48.5, yoff=3)
        with xr.open_dataset(sample_file) as dataset:
            shifted = dataset.assign_coords(latitude=[40.75], longitude=[286.0, 286.25]).load()
        sample_file = tmp_path / "nyc.nc"
        shifted.to_netcdf(sample_file, engine="netcdf4")
    task = TaskSpec(place_name=f"{place}, USA", heat_metric=heat_metric)
    ctx = Context()
    ctx.put("task", task)
    previews = tmp_path / "input_layers"
    ctx.put("config", {"output": {"input_layers": str(previews)}, "data": {
        "utci_path": "" if auto else str(sample_file), "utci_timezone": "America/Los_Angeles",
    }})
    monkeypatch.setattr(workflow, "cds_configured", lambda: auto)
    requests = []

    def fake_download(tracts, start, end, timezone, **kwargs):
        features = json.loads((previews / "tract.geojson").read_text(encoding="utf-8"))["features"]
        assert len(features) == len(tracts)
        assert features[0]["properties"]["population"] == 100
        requests.append((regional_boxes(tracts), start, end, timezone))
        return [sample_file]

    monkeypatch.setattr(workflow, "regional_utci_files", fake_download)
    ctx.put("study", {
        "place_name": task.place_name, "state_fips": "36" if place == "New York" else "06",
        "county_fips": "061" if place == "New York" else "075",
        "year": 2022, "cache_scope": "test_thermal", "place_type": "county",
    })
    census = pd.DataFrame({
        "GEOID": sf_tracts.GEOID, "total_population": [100, 100, 100],
        "m65_66": [10, 10, 10], "below_poverty": [5, 5, 5], "poverty_universe": [100, 100, 100],
    })
    monkeypatch.setattr(workflow, "fetch_acs", lambda *args: census)
    monkeypatch.setattr(workflow, "fetch_tract_geometry", lambda *args: sf_tracts)
    monkeypatch.setattr(workflow, "fetch_heat_exposure", lambda *args, **kwargs: (
        pd.DataFrame({"GEOID": sf_tracts.GEOID, "mean_lst": [34.0, 36.0, 41.0]}),
        Provenance(source="test Landsat", record_count=3),
    ))

    outcome = workflow.prepare_demand(ctx, start_date="2022-07-01", end_date="2022-07-02")
    assert outcome.ok, outcome.error
    assert len(requests) == int(auto)
    if auto:
        assert requests[0][0] == [[40.76, -74.01, 40.74, -73.74]]
    demand = ctx.get("demand")
    expected = [30, 30, 22] if heat_metric == "utci" else [34, 36, 41]
    np.testing.assert_allclose(demand["indicators"]["heat_exposure"], expected)

    map_path = tmp_path / "map.geojson"
    heat_exposure_map(demand["areas"], str(map_path))
    artifact = json.loads(map_path.read_text(encoding="utf-8"))
    assert artifact["metadata"]["run_heat_metric"] == heat_metric
    assert set(artifact["metadata"]["thermal_layers"]) == {"landsat_lst", "utci"}
    assert artifact["features"][0]["properties"]["utci_c"] == 30
    assert artifact["features"][0]["properties"]["lst_exposure_c"] == 34

    report = render_report(
        task, SiteSolution(
            profile_name="test", selected_ids=["site"], covered_population=100, covered_ratio=.5,
            vulnerable_covered_ratio=.5, mean_travel_minutes=10, weights_used=task.weights, solver_status="Optimal",
        ),
        [], ctx.lineage, model_context={"thermal": ctx.get("quality")["heat"]},
    )
    assert r"P_6(T_a" in report and r"\max_{h \in H_d}" in report
    assert "Bröde et al." in report and "0.25° native grid" in report
    assert ("UTCI comparison layer" in report) == (heat_metric == "landsat_lst")
    assert ("4.1 Outdoor thermal stress (UTCI)" in report) == (heat_metric == "utci")


def test_regional_download_requests_exact_dates_and_reuses_cache(tmp_path, sample_file, sf_tracts):
    calls = []

    class Response:
        def download(self, destination):
            from pathlib import Path
            Path(destination).write_bytes(sample_file.read_bytes())

    class Client:
        def retrieve(self, dataset, request):
            calls.append((dataset, request))
            return Response()

    boxes = regional_boxes(sf_tracts)
    dates = pd.date_range("2022-07-31", "2022-08-02", freq="D", tz="UTC")
    options = {"progress": lambda *_: None, "cancel_check": lambda: None, "client": Client()}
    files = download_utci(boxes, dates, tmp_path / "regional", **options)
    assert len(files) == 2
    assert calls[0][0] == "derived-utci-historical"
    assert [request["day"] for _, request in calls] == [["31"], ["01", "02"]]
    assert calls[0][1]["area"] == [37.76, -122.51, 37.74, -122.24]
    assert regional_boxes(sf_tracts.iloc[:1]) == [[37.76, -122.51, 37.74, -122.49]]
    assert download_utci(boxes, dates, tmp_path / "regional", **options) == files
    assert len(calls) == 2
    legacy = regional_boxes(sf_tracts, margin=.25)
    legacy_files = download_utci(legacy, dates, tmp_path / "legacy", **options)
    assert download_utci(boxes, dates, tmp_path / "legacy", legacy_boxes=legacy, **options) == legacy_files
    assert len(calls) == 4
