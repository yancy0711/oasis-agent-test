"""Offline tests for Landsat surface-temperature helpers and tract cache."""
import hashlib
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point, box

from oasis.core.data import heat


def test_strip_asset_signatures_refreshes_only_azure_assets():
    """Cached Azure raster URLs must not retain expiring SAS queries."""
    azure = SimpleNamespace(href="https://example.blob.core.windows.net/a/x.tif?st=old&se=old")
    public = SimpleNamespace(href="https://example.org/x.tif?version=1")
    item = SimpleNamespace(assets={"azure": azure, "public": public})
    assert heat._strip_asset_signatures([item]) == [item]
    assert azure.href == "https://example.blob.core.windows.net/a/x.tif"
    assert public.href.endswith("?version=1")


def test_readable_items_skips_a_non_tiff_asset(monkeypatch, caplog):
    """A corrupt catalog asset must not abort an otherwise usable composite."""
    import planetary_computer
    import requests

    good = SimpleNamespace(id="good", assets={band: SimpleNamespace(href=f"good-{band}") for band in ("lwir11", "qa_pixel")})
    bad = SimpleNamespace(id="bad", assets={band: SimpleNamespace(href=f"bad-{band}") for band in ("lwir11", "qa_pixel")})
    monkeypatch.setattr(planetary_computer, "sign", lambda url: url)
    monkeypatch.setattr(requests, "get", lambda url, **_: SimpleNamespace(
        content=b"<htm" if url == "bad-qa_pixel" else b"II*\x00", raise_for_status=lambda: None))
    assert heat._readable_items([good, bad]) == [good]
    assert "Skipping corrupt Landsat scene bad" in caplog.text


def test_temperature_scale_matches_usgs_formula():
    assert np.isclose(heat._temperature_celsius(44947), 29.45, atol=0.05)


def test_surface_reflectance_scale_matches_usgs_formula():
    assert np.isclose(heat._surface_reflectance(10000), 0.075)


def test_clear_mask_rejects_cloud_shadow_snow_and_water():
    qa = np.array([0, 1 << 3, 1 << 4, 1 << 5, 1 << 7], dtype="uint16")
    assert heat._clear_mask(qa).tolist() == [True, False, False, False, False]


def test_request_key_is_order_stable_and_parameter_sensitive():
    tracts = gpd.GeoDataFrame(
        {"GEOID": ["2", "1"]}, geometry=[Point(-118, 34), Point(-117, 35)], crs="EPSG:4326"
    )
    a = heat._request_key(tracts, resolution=120)
    assert a == heat._request_key(tracts.iloc[::-1], resolution=120)
    assert a != heat._request_key(tracts, resolution=60)


def test_fetch_heat_exposure_reuses_derived_cache(monkeypatch, tmp_path):
    tracts = gpd.GeoDataFrame(
        {"GEOID": ["06037101100"]}, geometry=[Point(-118, 34)], crs="EPSG:4326"
    )
    monkeypatch.setattr(heat, "cache_dir", lambda _: tmp_path)
    params = {
        "start_date": "2022-06-01", "end_date": "2022-08-31",
        "cloud_cover": 20, "resolution": 120,
        "bbox": tracts.total_bounds.astype(float).tolist(),
        "population_source": None, "tract_population": None, "population_areas": None,
    }
    path = tmp_path / f"landsat_lst_{heat._request_key(tracts, **params)}.csv"
    pd.DataFrame({"GEOID": ["06037101100"], "mean_lst": [35.0]}).to_csv(path, index=False)
    monkeypatch.setattr(heat, "_search_items", lambda *args: (_ for _ in ()).throw(AssertionError))

    result, provenance = heat.fetch_heat_exposure(tracts, "2022-06-01", "2022-08-31")
    assert provenance.tier == "cache"
    assert result.to_dict("records") == [{"GEOID": "06037101100", "mean_lst": 35.0}]


def test_population_weighting_refresh_failure_retains_cached_tract_heat(monkeypatch, tmp_path):
    tracts = gpd.GeoDataFrame(
        {"GEOID": ["06037101100"]}, geometry=[Point(-118, 34)], crs="EPSG:4326"
    )
    monkeypatch.setattr(heat, "cache_dir", lambda _: tmp_path)
    population_source = {"provider": "test_grid", "url": "population.tif", "year": 2022}
    population = np.array([100.0])
    params = {
        "start_date": "2022-06-01", "end_date": "2022-08-31",
        "cloud_cover": 20, "resolution": 120,
        "bbox": tracts.total_bounds.astype(float).tolist(),
        "population_source": "population.tif",
        "tract_population": hashlib.sha256(population.tobytes()).hexdigest()[:16],
        "population_areas": None,
    }
    path = tmp_path / f"landsat_lst_{heat._request_key(tracts, **params)}.csv"
    pd.DataFrame({
        "GEOID": ["06037101100"], "mean_lst": [35.0],
        "population_weighted_lst": [np.nan],
    }).to_csv(path, index=False)
    monkeypatch.setattr(
        heat, "_search_items",
        lambda *args: (_ for _ in ()).throw(ConnectionError("remote refresh unavailable")),
    )

    result, provenance = heat.fetch_heat_exposure(
        tracts, "2022-06-01", "2022-08-31",
        population_source=population_source, tract_population=population,
    )

    assert provenance.tier == "cache"
    assert result.loc[0, "mean_lst"] == 35.0
    assert "retained" in provenance.notes


def test_population_weighted_zonal_calibrates_grid_to_acs_total(monkeypatch):
    tracts = gpd.GeoDataFrame(
        {"GEOID": ["1"]}, geometry=[box(0, 0, 2, 2)], crs="EPSG:3857"
    )
    composite = SimpleNamespace(
        values=np.array([[10.0, 20.0], [30.0, 40.0]]),
        x=np.array([0.5, 1.5]), y=np.array([1.5, 0.5]),
    )
    monkeypatch.setattr(
        heat, "_read_population_on_grid",
        lambda *args: np.array([[90.0, 10.0], [0.0, 0.0]]),
    )

    result = heat._population_weighted_zonal(
        tracts, composite, "EPSG:3857", "population.tif", np.array([100.0])
    ).iloc[0]

    assert np.isclose(result["mean_lst"], 25.0)
    assert np.isclose(result["population_weighted_lst"], 11.0)
    assert np.isclose(result["exposure_gap"], -14.0)
    assert np.isclose(result["valid_lst_population_share"], 1.0)


def test_block_group_population_weights_landsat_cells_within_tract():
    tracts = gpd.GeoDataFrame(
        {"GEOID": ["1"]}, geometry=[box(0, 0, 2, 2)], crs="EPSG:3857"
    )
    block_groups = gpd.GeoDataFrame(
        {"GEOID": ["11", "12"], "population": [90.0, 10.0]},
        geometry=[box(0, 1, 2, 2), box(0, 0, 2, 1)], crs="EPSG:3857",
    )
    composite = SimpleNamespace(
        values=np.array([[10.0, 20.0], [30.0, 40.0]]),
        x=np.array([0.5, 1.5]), y=np.array([1.5, 0.5]),
    )
    result = heat._population_weighted_from_polygons(
        tracts, composite, "EPSG:3857", block_groups, np.array([100.0])
    ).iloc[0]
    assert np.isclose(result["mean_lst"], 25.0)
    assert np.isclose(result["population_weighted_lst"], 17.0)
    assert np.isclose(result["valid_lst_population_share"], 1.0)


def test_landsat_probe_retries_a_transient_ssl_failure(monkeypatch):
    """One Azure EOF must be retried without aborting every scene probe."""
    import planetary_computer
    import requests

    item = SimpleNamespace(
        id="retry",
        assets={
            band: SimpleNamespace(href=f"https://blob.example/{band}.tif")
            for band in ("lwir11", "qa_pixel")
        },
    )
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise requests.exceptions.SSLError("unexpected EOF")
        return SimpleNamespace(content=b"II*\x00", raise_for_status=lambda: None)

    monkeypatch.setattr(planetary_computer, "sign", lambda url: url)
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(heat.time, "sleep", lambda _: None)
    assert heat._item_is_readable(item)
    assert len(calls) == 3
