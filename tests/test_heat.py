"""Offline tests for Landsat surface-temperature helpers and tract cache."""
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

from tools.data_acquisition import heat


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
    }
    path = tmp_path / f"landsat_lst_{heat._request_key(tracts, **params)}.csv"
    pd.DataFrame({"GEOID": ["06037101100"], "mean_lst": [35.0]}).to_csv(path, index=False)
    monkeypatch.setattr(heat, "_search_items", lambda *args: (_ for _ in ()).throw(AssertionError))

    result, provenance = heat.fetch_heat_exposure(tracts, "2022-06-01", "2022-08-31")
    assert provenance.tier == "cache"
    assert result.to_dict("records") == [{"GEOID": "06037101100", "mean_lst": 35.0}]
