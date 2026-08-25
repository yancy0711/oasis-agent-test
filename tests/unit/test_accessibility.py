"""Offline tests for Haversine, ORS block assembly, and fallback behavior."""
import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import Point

from oasis.core.spatial import accessibility
from oasis.core.spatial.accessibility import backend as routing_backend
from oasis.core.spatial.accessibility import builder as accessibility_builder


def _patch_backend(monkeypatch, function):
    """Inject one deterministic rectangular backend without HTTP or SQLite coupling."""
    monkeypatch.setattr(
        accessibility.OrsBackend,
        "fetch_block",
        lambda self, sources, destinations, **kwargs: function(
            sources, destinations, **kwargs
        ),
    )


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
    monkeypatch.setattr(accessibility_builder, "cache_dir", lambda _: tmp_path)
    demand = _gdf([(-118.0, 34.0), (-118.01, 34.01)])
    supply = _gdf([(-118.02, 34.02), (-118.03, 34.03)])
    mask = np.array([[1, 0], [0, 1]], dtype=bool)

    def fake_block(demand_coords, supply_coords, **kwargs):
        return np.full((len(demand_coords), len(supply_coords)), 7.0)

    _patch_backend(monkeypatch, fake_block)
    progress = []
    first, report = accessibility.sparse_network_matrix(
        demand, supply, mask, api_key="test", request_interval_s=0,
        source_chunk_size=2, max_routes_per_request=4,
        progress_callback=lambda label, done, total: progress.append((label, done, total)),
    )
    assert report["router"] == "ors_sparse" and report["live_pairs"] == 2
    assert first[0, 0] == first[1, 1] == 7 and np.isinf(first[0, 1])
    assert ("ORS cache scan", 2, 2) in progress and progress[-1] == ("ORS requests", 1, 1)

    _patch_backend(monkeypatch, lambda *args, **kwargs: pytest.fail("API called"))
    second, report = accessibility.sparse_network_matrix(demand, supply, mask, request_interval_s=0)
    assert report["tier"] == "cache" and np.array_equal(first, second)


def test_sparse_matrix_falls_back_when_request_guard_is_exceeded(monkeypatch, tmp_path):
    monkeypatch.setattr(accessibility_builder, "cache_dir", lambda _: tmp_path)
    demand = _gdf([(-118.0, 34.0), (-118.1, 34.1)])
    supply = _gdf([(-118.2, 34.2), (-118.3, 34.3)])
    mask = np.ones((2, 2), dtype=bool)
    fallback = np.full((2, 2), 9.0)
    matrix, report = accessibility.sparse_network_matrix(
        demand, supply, mask, fallback_matrix=fallback, api_key="test",
        source_chunk_size=1, max_routes_per_request=1, max_requests=1,
    )
    assert report["router"] == "haversine" and "exceed automatic guard" in report["reason"]
    assert np.array_equal(matrix, fallback)


def test_progress_bar_reports_ors_wait_state(capsys):
    """Verify ORS progress output includes its label and completed count."""
    accessibility_builder.print_progress("ORS requests", 2, 4)
    output = capsys.readouterr().out
    assert "ORS requests" in output and "2/4" in output


def test_sparse_planner_respects_destination_chunk_size():
    demand = _gdf([(-118.0, 34.0), (-118.001, 34.001)])
    mask = np.ones((2, 5), dtype=bool)
    blocks = accessibility.plan_sparse_blocks(
        demand,
        mask,
        source_chunk_size=2,
        max_routes_per_request=100,
        destination_chunk_size=2,
    )
    assert blocks
    assert all(len(destinations) <= 2 for _, destinations in blocks)


def test_complete_matrix_cache_skips_pair_scan_and_remote(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(accessibility_builder, "cache_dir", lambda _: tmp_path)
    demand = _gdf([(-118.0, 34.0), (-118.01, 34.01)])
    supply = _gdf([(-118.02, 34.02), (-118.03, 34.03)])
    mask = np.eye(2, dtype=bool)

    _patch_backend(
        monkeypatch,
        lambda demand_coords, supply_coords, **kwargs: np.full(
            (len(demand_coords), len(supply_coords)), 6.0
        ),
    )
    first, first_report = accessibility.sparse_network_matrix(
        demand,
        supply,
        mask,
        api_key="test",
        request_interval_s=0,
        source_chunk_size=2,
        max_routes_per_request=4,
    )
    assert first_report["live_pairs"] == 2
    assert list((tmp_path / "matrices").glob("matrix_*.npz"))

    monkeypatch.setattr(
        accessibility_builder.CacheStore,
        "lookup_pairs",
        lambda *args, **kwargs: pytest.fail("SQLite pair scan used"),
    )
    _patch_backend(
        monkeypatch,
        lambda *args, **kwargs: pytest.fail("remote ORS used"),
    )
    second, second_report = accessibility.sparse_network_matrix(
        demand, supply, mask, request_interval_s=0
    )
    assert second_report["matrix_cache_hit"]
    assert second_report["cache_level"] == "matrix"
    assert np.array_equal(first, second)


def test_sparse_requests_respect_worker_and_inflight_limits(
    monkeypatch, tmp_path
):
    import threading
    import time

    monkeypatch.setattr(accessibility_builder, "cache_dir", lambda _: tmp_path)
    demand = _gdf(
        [(-118.0 + i * 0.001, 34.0 + i * 0.001) for i in range(4)]
    )
    supply = _gdf(
        [(-118.01 + i * 0.001, 34.01 + i * 0.001) for i in range(4)]
    )
    mask = np.ones((4, 4), dtype=bool)
    lock = threading.Lock()
    active_calls = active_routes = 0
    maximum_calls = maximum_routes = 0

    def fake_block(demand_coords, supply_coords, **kwargs):
        nonlocal active_calls, active_routes, maximum_calls, maximum_routes
        routes = len(demand_coords) * len(supply_coords)
        with lock:
            active_calls += 1
            active_routes += routes
            maximum_calls = max(maximum_calls, active_calls)
            maximum_routes = max(maximum_routes, active_routes)
        time.sleep(0.03)
        with lock:
            active_calls -= 1
            active_routes -= routes
        return np.full((len(demand_coords), len(supply_coords)), 5.0)

    _patch_backend(monkeypatch, fake_block)
    matrix, report = accessibility.sparse_network_matrix(
        demand,
        supply,
        mask,
        api_key="test",
        request_interval_s=0,
        source_chunk_size=2,
        destination_chunk_size=2,
        max_routes_per_request=4,
        max_concurrency=2,
        max_inflight_routes=8,
    )
    assert np.all(matrix == 5)
    assert report["completed_requests"] == report["planned_requests"]
    assert maximum_calls == 2
    assert maximum_routes <= 8


def test_retry_after_header_controls_retry_delay(monkeypatch):
    from oasis.core import retry

    class Response:
        def __init__(self, status_code, headers=None):
            self.status_code = status_code
            self.headers = headers or {}
            self.ok = status_code == 200
            self.text = ""

        def json(self):
            return {"durations": [[60.0]]}

    responses = iter(
        [Response(429, {"Retry-After": "3"}), Response(200)]
    )
    sleeps = []
    monkeypatch.setattr(
        routing_backend.requests, "post", lambda *args, **kwargs: next(responses)
    )
    monkeypatch.setattr(retry.time, "sleep", sleeps.append)
    result = routing_backend.post_ors(
        "https://example.test/matrix/foot-walking",
        "test",
        {"locations": []},
        1,
    )
    assert result["durations"] == [[60.0]]
    assert sleeps and sleeps[0] >= 3


def test_cancelled_sparse_run_commits_completed_block_for_resume(
    monkeypatch, tmp_path
):
    from oasis.core.cancellation import CancellationToken, RunCancelled

    monkeypatch.setattr(accessibility_builder, "cache_dir", lambda _: tmp_path)
    demand = _gdf([(-118.0, 34.0), (-118.001, 34.001)])
    supply = _gdf([(-118.01, 34.01), (-118.011, 34.011)])
    mask = np.ones((2, 2), dtype=bool)
    token = CancellationToken()
    first_calls = 0

    def cancel_after_block(demand_coords, supply_coords, **kwargs):
        nonlocal first_calls
        first_calls += 1
        token.cancel()
        return np.full((len(demand_coords), len(supply_coords)), 4.0)

    _patch_backend(monkeypatch, cancel_after_block)
    with pytest.raises(RunCancelled):
        accessibility.sparse_network_matrix(
            demand,
            supply,
            mask,
            api_key="test",
            request_interval_s=0,
            source_chunk_size=1,
            destination_chunk_size=1,
            max_routes_per_request=1,
            max_concurrency=1,
            cancel_check=token.check,
        )
    assert first_calls == 1
    assert not list((tmp_path / "matrices").glob("matrix_*.npz"))

    resumed_calls = 0

    def resume_block(demand_coords, supply_coords, **kwargs):
        nonlocal resumed_calls
        resumed_calls += 1
        return np.full((len(demand_coords), len(supply_coords)), 4.0)

    _patch_backend(monkeypatch, resume_block)
    matrix, report = accessibility.sparse_network_matrix(
        demand,
        supply,
        mask,
        api_key="test",
        request_interval_s=0,
        source_chunk_size=1,
        destination_chunk_size=1,
        max_routes_per_request=1,
        max_concurrency=1,
    )
    assert np.all(matrix == 4)
    assert report["cache_pairs"] == 1
    assert resumed_calls == 3
