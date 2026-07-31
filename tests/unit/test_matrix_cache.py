"""Direct tests for passive cache keys and legacy ORS pair reuse."""
from __future__ import annotations

import numpy as np

from oasis.core.spatial.accessibility.backend import RoutingIdentity
from oasis.core.spatial.accessibility.builder import (
    PairRequest,
    build_matrix_key,
    build_pair_key,
)
from oasis.core.spatial.accessibility.cache import CacheStore


def test_passive_pair_cache_accepts_builder_constructed_keys(tmp_path):
    """The cache stores and retrieves keys without deriving eligibility or coordinates."""
    identity = RoutingIdentity(
        provider="ors",
        profile="foot-walking",
        endpoint="https://ors.example/matrix",
        version="test-v1",
    )
    key = build_pair_key(identity, (1.0, 2.0), (3.0, 4.0))
    cache = CacheStore(tmp_path)
    cache.store_pairs([(key, 7.5)])
    cache.close()

    reopened = CacheStore(tmp_path)
    assert reopened.lookup_pairs([PairRequest(2, 4, key)]) == [(2, 4, 7.5)]
    reopened.close()


def test_keys_isolate_matrix_masks_and_non_ors_metrics():
    """Full matrices include masks, while pair storage isolates future semantics."""
    identity = RoutingIdentity(
        provider="ors",
        profile="foot-walking",
        endpoint="https://ors.example/matrix",
        version="v2",
    )
    coordinates = [(1.0, 2.0)]
    included = build_matrix_key(
        identity, coordinates, coordinates, np.ones((1, 1), dtype=bool), 5
    )
    excluded = build_matrix_key(
        identity, coordinates, coordinates, np.zeros((1, 1), dtype=bool), 5
    )
    assert included != excluded
    assert build_pair_key(identity, coordinates[0], coordinates[0]).storage_version == "v2"

    distance_identity = RoutingIdentity(
        provider="custom",
        profile="foot-walking",
        endpoint="https://ors.example/matrix",
        version="v2",
        metric="distance_metres",
    )
    assert (
        build_pair_key(
            distance_identity, coordinates[0], coordinates[0]
        ).storage_version
        != "v2"
    )
