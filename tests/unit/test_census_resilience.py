"""Regression tests for transient Census API payload failures."""
from __future__ import annotations

from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import geopandas as gpd
import pytest
import requests
from shapely.geometry import Point

from oasis.core.data import census
from oasis.core.retry import TransientError


def test_empty_census_table_is_retryable_instead_of_index_error(monkeypatch, tmp_path):
    """An empty provider payload must never reach rows[0]."""
    monkeypatch.setattr(census, "cache_dir", lambda _: tmp_path)
    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: [],
        ),
    )

    with pytest.raises(TransientError, match="no tract rows"):
        census._get.__wrapped__("https://census.example", {"for": "tract:*"})


def test_census_transport_error_is_retryable(monkeypatch, tmp_path):
    """SSL and connection failures must retain provider context."""
    monkeypatch.setattr(census, "cache_dir", lambda _: tmp_path)

    def fail(*args, **kwargs):
        raise requests.exceptions.SSLError("unexpected EOF")

    monkeypatch.setattr(requests, "get", fail)
    with pytest.raises(TransientError, match="Census API request failed"):
        census._get.__wrapped__("https://census.example", {"for": "tract:*"})


def test_state_block_groups_are_loaded_once_across_county_threads(monkeypatch):
    """Concurrent counties in one state must not share a disposable temporary ZIP."""
    calls = []
    frame = gpd.GeoDataFrame(
        {"COUNTYFP": ["047", "061"], "GEOID": ["360470001001", "360610001001"]},
        geometry=[Point(-73.9, 40.7), Point(-74.0, 40.8)], crs="EPSG:4326",
    )

    def load(**kwargs):
        calls.append(kwargs)
        return frame

    census._BLOCK_GROUP_STATE_CACHE.clear()
    census._BLOCK_GROUP_LOCKS.clear()
    monkeypatch.setattr(census.pygris, "block_groups", load)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: census._state_block_groups("36", 2022), range(2)))

    assert len(calls) == 1
    assert calls[0]["cache"] is True
    assert all(len(result) == 2 for result in results)


def test_geopackage_cache_writes_use_safe_explicit_layer_names(monkeypatch, tmp_path):
    """Temporary filenames must never become implicit GeoPackage layer names."""
    frame = gpd.GeoDataFrame(
        {"COUNTYFP": ["095"], "GEOID": ["12095000100"]},
        geometry=[Point(-81.38, 28.54)], crs="EPSG:4326",
    )
    layers = []

    def write_file(_frame, filename, **kwargs):
        layers.append(kwargs.get("layer"))
        Path(filename).write_bytes(b"test")

    monkeypatch.setattr(census, "cache_dir", lambda _name: tmp_path)
    monkeypatch.setattr(census.pygris, "tracts", lambda **_kwargs: frame.copy())
    monkeypatch.setattr(census.pygris, "counties", lambda **_kwargs: frame[["COUNTYFP", "geometry"]].copy())
    monkeypatch.setattr(gpd.GeoDataFrame, "to_file", write_file)
    census.fetch_tract_geometry("12", "095")
    census.fetch_county_geometry("12")
    assert layers == ["tracts", "counties"]
