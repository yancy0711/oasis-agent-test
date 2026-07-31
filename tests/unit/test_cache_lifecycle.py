"""Lifecycle tests for durable route caches."""
from __future__ import annotations

import os
import sqlite3
import time

import numpy as np

from oasis.core.spatial.accessibility.backend import RoutingIdentity
from oasis.core.spatial.accessibility.builder import PairRequest, build_pair_key
from oasis.core.spatial.accessibility.cache import CacheStore

IDENTITY = RoutingIdentity(
    provider="ors",
    profile="foot-walking",
    endpoint="https://ors.example/matrix",
    version="test-v1",
)


def _cache(tmp_path, **kwargs) -> CacheStore:
    """Create one isolated passive cache."""
    return CacheStore(tmp_path, **kwargs)


def test_complete_matrix_expires_by_ttl(tmp_path):
    """An expired NPZ must be ignored and removed rather than silently reused."""
    cache = _cache(tmp_path, ttl_days=1)
    matrix = np.asarray([[4.0]])
    path = cache.save_matrix("key", matrix, np.ones((1, 1), dtype=bool), {})
    old = time.time() - 2 * 86_400
    os.utime(path, (old, old))

    assert cache.load_matrix("key", (1, 1)) is None
    assert not path.exists()


def test_pair_rows_expire_and_completed_rows_are_durable(tmp_path):
    """Pair checkpoints survive reopening but respect the configured TTL."""
    key = build_pair_key(IDENTITY, (1.0, 2.0), (3.0, 4.0))
    request = PairRequest(0, 0, key)

    cache = _cache(tmp_path, ttl_days=1)
    cache.store_pairs([(key, 7.5)])
    cache.close()
    reopened = _cache(tmp_path, ttl_days=1)
    assert reopened.lookup_pairs([request]) == [(0, 0, 7.5)]
    reopened.close()

    with sqlite3.connect(tmp_path / "pair_durations.sqlite") as connection:
        connection.execute("UPDATE durations SET updated_at=1")
    expired = _cache(tmp_path, ttl_days=1)
    assert expired.lookup_pairs([request]) == []
    expired.close()


def test_file_budget_prunes_old_matrix_but_ignores_unowned_raw_files(tmp_path):
    """Capacity pruning owns matrix files only and never reaches into backend files."""
    old_matrix = tmp_path / "matrices" / "matrix_old.npz"
    old_matrix.parent.mkdir(parents=True)
    old_matrix.write_bytes(b"x" * 1024)
    raw = tmp_path / "legacy_raw.json"
    raw.write_bytes(b"legacy")
    old = time.time() - 100
    os.utime(old_matrix, (old, old))
    cache = _cache(tmp_path, max_size_gb=1e-9)
    path = cache.save_matrix(
        "key",
        np.asarray([[4.0]]),
        np.ones((1, 1), dtype=bool),
        {},
    )

    assert path.exists()
    assert not old_matrix.exists()
    assert raw.exists()
