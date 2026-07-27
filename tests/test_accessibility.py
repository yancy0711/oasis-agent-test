"""Offline tests for Haversine, ORS block assembly, and fallback behavior."""
import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import Point

from tools.spatial import accessibility


def _gdf(coords):
    return gpd.GeoDataFrame(
        geometry=[Point(x, y) for x, y in coords],
        crs="EPSG:4326",
    )


def test_known_haversine_distance():
    demand = _gdf([(-118.0, 34.0)])
    supply = _gdf([(-118.01, 34.0)])
    minutes = accessibility.haversine_matrix(
        demand, supply, speed_kmh=4.8, detour_factor=1.0
    )[0, 0]
    assert 10 < minutes < 13


def test_zero_distance_is_zero():
    point = _gdf([(-118.0, 34.0)])
    assert accessibility.haversine_matrix(point, point)[0, 0] == 0


def test_matrix_validation_rejects_nan_and_wrong_shape():
    with pytest.raises(ValueError, match="shape"):
        accessibility.validate_travel_matrix(np.ones((2, 2)), 2, 3)
    with pytest.raises(ValueError, match="NaN"):
        accessibility.validate_travel_matrix(np.array([[np.nan]]), 1, 1)


def test_network_matrix_stitches_blocks(monkeypatch):
    calls = []

    def fake_block(
        demand_coords,
        supply_coords,
        *,
        profile,
        api_key,
        base_url,
        timeout_s,
    ):
        calls.append((len(demand_coords), len(supply_coords), profile))
        return np.full((len(demand_coords), len(supply_coords)), len(calls))

    monkeypatch.setattr(accessibility, "_ors_block", fake_block)
    demand = _gdf([(-118.0, 34.0), (-118.1, 34.1), (-118.2, 34.2)])
    supply = _gdf([(-118.3, 34.3), (-118.4, 34.4), (-118.5, 34.5)])
    matrix = accessibility.network_matrix(
        demand,
        supply,
        api_key="test-key",
        source_chunk_size=2,
        destination_chunk_size=2,
        max_routes_per_request=4,
        max_requests=4,
    )

    assert matrix.shape == (3, 3)
    assert calls == [
        (2, 2, "foot-walking"),
        (2, 1, "foot-walking"),
        (1, 2, "foot-walking"),
        (1, 1, "foot-walking"),
    ]
    assert np.array_equal(matrix, [[1, 1, 2], [1, 1, 2], [3, 3, 4]])


def test_sparse_planner_covers_mask_without_exceeding_route_limit():
    demand = _gdf([(-118.0, 34.0), (-118.001, 34.001), (-118.1, 34.1)])
    mask = np.array([[1, 1, 0], [0, 1, 1], [0, 0, 1]], dtype=bool)
    blocks = accessibility.plan_sparse_blocks(demand, mask, source_chunk_size=2, max_routes_per_request=4)
    covered = np.zeros_like(mask)
    for sources, destinations in blocks:
        covered[np.ix_(sources, destinations)] = True
        assert len(sources) * len(destinations) <= 4
    assert covered[mask].all()


def test_sparse_matrix_uses_pair_cache_on_second_run(monkeypatch, tmp_path):
    monkeypatch.setattr(accessibility, "cache_dir", lambda _: tmp_path)
    demand = _gdf([(-118.0, 34.0), (-118.01, 34.01)])
    supply = _gdf([(-118.02, 34.02), (-118.03, 34.03)])
    mask = np.array([[1, 0], [0, 1]], dtype=bool)

    def fake_block(demand_coords, supply_coords, **kwargs):
        return np.full((len(demand_coords), len(supply_coords)), 7.0)

    monkeypatch.setattr(accessibility, "_ors_block", fake_block)
    progress = []
    first, report = accessibility.sparse_network_matrix(
        demand, supply, mask, api_key="test", request_interval_s=0,
        source_chunk_size=2, max_routes_per_request=4,
        progress_callback=lambda label, done, total: progress.append((label, done, total)),
    )
    assert report["router"] == "ors_sparse" and report["live_pairs"] == 2
    assert first[0, 0] == first[1, 1] == 7 and np.isinf(first[0, 1])
    assert ("ORS cache scan", 2, 2) in progress and progress[-1] == ("ORS requests", 1, 1)

    monkeypatch.setattr(accessibility, "_ors_block", lambda *a, **k: pytest.fail("API called"))
    second, report = accessibility.sparse_network_matrix(demand, supply, mask, request_interval_s=0)
    assert report["tier"] == "cache" and np.array_equal(first, second)


def test_sparse_matrix_falls_back_when_request_guard_is_exceeded(monkeypatch, tmp_path):
    monkeypatch.setattr(accessibility, "cache_dir", lambda _: tmp_path)
    demand = _gdf([(-118.0, 34.0), (-118.1, 34.1)])
    supply = _gdf([(-118.2, 34.2), (-118.3, 34.3)])
    mask = np.ones((2, 2), dtype=bool)
    fallback = np.full((2, 2), 9.0)
    matrix, report = accessibility.sparse_network_matrix(
        demand, supply, mask, fallback_matrix=fallback, api_key="test",
        source_chunk_size=1, max_routes_per_request=1, max_requests=1,
    )
    assert report["router"] == "haversine" and "exceed guard" in report["reason"]
    assert np.array_equal(matrix, fallback)


def test_ors_block_converts_seconds_and_null(monkeypatch, tmp_path):
    monkeypatch.setattr(accessibility, "cache_dir", lambda _: tmp_path)
    monkeypatch.setattr(
        accessibility,
        "_post_ors",
        lambda *args: {"durations": [[60.0, None], [120.0, 180.0]]},
    )
    matrix = accessibility._ors_block(
        [[-118.0, 34.0], [-118.1, 34.1]],
        [[-118.2, 34.2], [-118.3, 34.3]],
        profile="foot-walking",
        api_key="test-key",
        base_url="https://example.test/matrix",
        timeout_s=1,
    )
    assert matrix[0, 0] == 1.0
    assert np.isinf(matrix[0, 1])
    assert matrix[1, 1] == 3.0


def test_travel_time_matrix_falls_back_when_ors_fails(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(accessibility, "network_matrix", fail)
    point = _gdf([(-118.0, 34.0)])
    matrix, router = accessibility.travel_time_matrix(
        point,
        point,
        {"router": "ors", "speed_kmh": 4.8, "detour_factor": 1.3},
    )
    assert router == "haversine"
    assert matrix[0, 0] == 0


def test_progress_bar_reports_ors_wait_state(capsys):
    """Verify ORS progress output includes its label and completed count."""
    accessibility._print_progress("ORS requests", 2, 4)
    output = capsys.readouterr().out
    assert "ORS requests" in output and "2/4" in output
