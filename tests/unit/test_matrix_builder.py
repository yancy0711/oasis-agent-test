"""Unit tests for matrix orchestration with no HTTP or SQLite dependencies."""
from __future__ import annotations

import numpy as np
import geopandas as gpd
import pytest
from shapely.geometry import Point

from oasis.core.cancellation import CancellationToken, RunCancelled
from oasis.core.spatial.accessibility.builder import TravelTimeMatrixBuilder
from oasis.core.spatial.accessibility.backend import RoutingIdentity


def _gdf(coords):
    return gpd.GeoDataFrame(
        geometry=[Point(longitude, latitude) for longitude, latitude in coords],
        crs="EPSG:4326",
    )


class MemoryCache:
    """Implement the passive cache protocol entirely in memory."""

    def __init__(self):
        self.matrices = {}
        self.pairs = {}
        self.stored_batches = []

    def load_matrix(self, key, expected_shape):
        matrix = self.matrices.get(key)
        return None if matrix is None else matrix.copy()

    def save_matrix(self, key, matrix, eligible_mask, metadata):
        self.matrices[key] = np.asarray(matrix).copy()

    def lookup_pairs(self, requests, *, cancel_check=None):
        if cancel_check:
            cancel_check()
        return [
            (request.row, request.column, self.pairs[request.key])
            for request in requests
            if request.key in self.pairs
        ]

    def store_pairs(self, values):
        batch = list(values)
        self.stored_batches.append(batch)
        self.pairs.update(batch)


class FakeBackend:
    """Return deterministic blocks and expose one stable cache identity."""

    def __init__(self, value=5.0, on_fetch=None):
        self.value = value
        self.on_fetch = on_fetch
        self.calls = 0
        self.identity = RoutingIdentity(
            provider="fake",
            profile="walking",
            endpoint="memory://router",
            version="test-v1",
        )

    def fetch_block(self, sources, destinations, *, cancel_check=None):
        self.calls += 1
        if self.on_fetch:
            self.on_fetch()
        return np.full((len(sources), len(destinations)), self.value)


def test_builder_plans_and_reuses_without_http_or_sqlite():
    """The builder can be tested through protocols without concrete I/O."""
    demand = _gdf([(-118.0, 34.0), (-118.01, 34.01)])
    supply = _gdf([(-118.02, 34.02), (-118.03, 34.03)])
    mask = np.eye(2, dtype=bool)
    cache = MemoryCache()
    first_backend = FakeBackend()
    first = TravelTimeMatrixBuilder(
        first_backend,
        cache,
        source_chunk_size=2,
        max_routes_per_request=4,
        max_concurrency=1,
        max_inflight_routes=4,
    )

    matrix, report = first.build(demand, supply, mask, allow_fallback=False)

    assert report["live_pairs"] == 2
    assert first_backend.calls == 1
    assert matrix[0, 0] == matrix[1, 1] == 5
    assert np.isinf(matrix[0, 1])

    second_backend = FakeBackend(on_fetch=lambda: pytest.fail("backend called"))
    second = TravelTimeMatrixBuilder(second_backend, cache)
    reused, reused_report = second.build(
        demand, supply, mask, allow_fallback=False
    )
    assert reused_report["matrix_cache_hit"]
    assert np.array_equal(matrix, reused)


def test_builder_checkpoints_completed_pair_before_cancellation():
    """A completed backend block is cached before cancellation is observed."""
    token = CancellationToken()
    cache = MemoryCache()
    backend = FakeBackend(on_fetch=token.cancel)
    builder = TravelTimeMatrixBuilder(
        backend,
        cache,
        source_chunk_size=1,
        destination_chunk_size=1,
        max_routes_per_request=1,
        max_concurrency=1,
        max_inflight_routes=1,
        cancel_check=token.check,
    )
    demand = _gdf([(-118.0, 34.0), (-118.01, 34.01)])
    supply = _gdf([(-118.02, 34.02), (-118.03, 34.03)])

    with pytest.raises(RunCancelled):
        builder.build(
            demand,
            supply,
            np.ones((2, 2), dtype=bool),
            allow_fallback=False,
        )

    assert backend.calls == 1
    assert len(cache.stored_batches) == 1
    assert len(cache.pairs) == 1

