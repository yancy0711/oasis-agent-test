"""Internal orchestration for one resumable travel-time matrix computation."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
import os
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from inspect import Parameter, signature
import logging
from typing import Any, Callable, Sequence

import geopandas as gpd
import numpy as np


from oasis.core.cache import cache_dir
from oasis.core.cancellation import RunCancelled
from oasis.core.spatial.alignment import pick_metric_crs
from .backend import ORS_BASE_URL, OrsBackend, RouterBackend, RoutingIdentity
from .cache import CacheStore
from .distance import haversine_matrix, point_coordinates, validate_travel_matrix

log = logging.getLogger(__name__)
@dataclass(frozen=True)
class PairKey:
    """Describe one directed source-destination routing value."""

    identity: RoutingIdentity
    source: tuple[float, float]
    destination: tuple[float, float]

    @property
    def storage_version(self) -> str:
        """Keep legacy ORS rows reusable while isolating future providers/metrics."""
        if self.identity.provider == "ors" and self.identity.metric == "duration_minutes":
            return self.identity.version
        suffix = hashlib.sha256(
            f"{self.identity.provider}\0{self.identity.metric}".encode("utf-8")
        ).hexdigest()[:12]
        return f"{self.identity.version}:{suffix}"


@dataclass(frozen=True)
class PairRequest:
    """Map one passive pair key back to its matrix cell."""

    row: int
    column: int
    key: PairKey


def build_pair_key(
    identity: RoutingIdentity,
    source: tuple[float, float],
    destination: tuple[float, float],
) -> PairKey:
    """Build one directed pair key outside the persistence layer."""
    return PairKey(identity, source, destination)


def build_matrix_key(
    identity: RoutingIdentity,
    demand_keys: Sequence[tuple[float, float]],
    supply_keys: Sequence[tuple[float, float]],
    eligible_mask: np.ndarray,
    coordinate_precision: int,
) -> str:
    """Hash ordered coordinates, routing identity, and the verified pair mask."""
    digest = hashlib.sha256()
    if identity.provider == "ors" and identity.metric == "duration_minutes":
        # Preserve v1/v2 ORS filenames so existing place caches still hit.
        for value in (
            identity.version,
            identity.profile,
            identity.endpoint.rstrip("/"),
            str(coordinate_precision),
        ):
            digest.update(value.encode("utf-8"))
            digest.update(b"\0")
    else:
        digest.update(
            json.dumps(
                {
                    "identity": identity.canonical(),
                    "coordinate_precision": coordinate_precision,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    for values, dtype in (
        (demand_keys, "<f8"),
        (supply_keys, "<f8"),
        (np.asarray(eligible_mask).shape, "<i8"),
    ):
        array = np.ascontiguousarray(values, dtype=dtype)
        digest.update(np.asarray(array.shape, dtype="<i8").tobytes())
        digest.update(array.tobytes())
    digest.update(
        np.packbits(
            np.asarray(eligible_mask, dtype=np.uint8).reshape(-1),
            bitorder="little",
        ).tobytes()
    )
    return digest.hexdigest()[:32]


def plan_sparse_blocks(
    demand: gpd.GeoDataFrame,
    eligible_mask: np.ndarray,
    source_chunk_size: int = 25,
    tile_size_m: float | None = 3000,
    max_routes_per_request: int = 2500,
    destination_chunk_size: int | None = None,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Cover sparse pairs with bounded rectangular route blocks."""
    mask = np.asarray(eligible_mask, dtype=bool)
    if mask.ndim != 2 or mask.shape[0] != len(demand):
        raise ValueError("eligible_mask rows must match demand")
    if source_chunk_size <= 0 or max_routes_per_request <= 0:
        raise ValueError("sparse block parameters must be positive")
    if tile_size_m is not None and tile_size_m <= 0:
        raise ValueError("tile_size_m must be positive when provided")
    if destination_chunk_size is not None and destination_chunk_size <= 0:
        raise ValueError("destination_chunk_size must be positive")

    tiles: dict[tuple[int, int], list[int]] = defaultdict(list)
    if tile_size_m is None:
        tiles[(0, 0)] = np.flatnonzero(mask.any(axis=1)).astype(int).tolist()
    else:
        points = demand.to_crs(pick_metric_crs(demand)).geometry
        x, y = points.x.to_numpy(), points.y.to_numpy()
        tx = np.floor((x - x.min()) / tile_size_m)
        ty = np.floor((y - y.min()) / tile_size_m)
        for index, tile in enumerate(zip(tx.astype(int), ty.astype(int))):
            if mask[index].any():
                tiles[tile].append(index)

    blocks = []
    for indices in tiles.values():
        for start in range(0, len(indices), source_chunk_size):
            sources = np.asarray(indices[start:start + source_chunk_size], dtype=int)
            destinations = np.flatnonzero(mask[sources].any(axis=0))
            size = max(1, max_routes_per_request // len(sources))
            if destination_chunk_size is not None:
                size = min(size, destination_chunk_size)
            blocks.extend(
                (sources, destinations[offset:offset + size])
                for offset in range(0, len(destinations), size)
            )
    return blocks


def print_progress(label: str, done: int, total: int) -> None:
    """Print one in-place progress bar for cache scans and live requests."""
    total = max(total, 1)
    width = 24
    filled = min(width, int(width * done / total))
    bar = "#" * filled + "-" * (width - filled)
    print(
        f"\r{label} [{bar}] {done}/{total}",
        end="\n" if done >= total else "",
        flush=True,
    )


class TravelTimeMatrixBuilder:
    """Build one matrix from passive cache lookups and bounded backend blocks."""

    def __init__(
        self,
        backend: RouterBackend,
        cache: CacheStore,
        *,
        source_chunk_size: int = 25,
        destination_chunk_size: int | None = None,
        tile_size_m: float | None = 3000,
        max_routes_per_request: int = 2500,
        max_requests: int = 500,
        max_concurrency: int = 2,
        max_inflight_routes: int = 3500,
        coordinate_precision: int = 5,
        progress_callback=None,
        cancel_check: Callable[[], None] | None = None,
    ):
        if coordinate_precision < 0:
            raise ValueError("coordinate_precision must be non-negative")
        if not 1 <= max_concurrency <= 2:
            raise ValueError("max_concurrency must be between 1 and 2")
        if not 1 <= max_routes_per_request <= 3500:
            raise ValueError("max_routes_per_request must be between 1 and 3500")
        if not 1 <= max_inflight_routes <= 3500:
            raise ValueError("max_inflight_routes must be between 1 and 3500")
        if max_requests < 1:
            raise ValueError("max_requests must be positive")
        self.backend = backend
        self.cache = cache
        self.source_chunk_size = source_chunk_size
        self.destination_chunk_size = destination_chunk_size
        self.tile_size_m = tile_size_m
        self.max_routes_per_request = max_routes_per_request
        self.max_requests = max_requests
        self.max_concurrency = max_concurrency
        self.max_inflight_routes = max_inflight_routes
        self.coordinate_precision = coordinate_precision
        self.progress_callback = progress_callback
        self.cancel_check = cancel_check
        self._inflight_routes = 0

    def _progress(self, label: str, done: int, total: int, report: dict[str, Any]) -> None:
        print_progress(label, done, total)
        if not self.progress_callback:
            return
        try:
            parameters = signature(self.progress_callback).parameters.values()
            accepts_stats = any(
                item.kind == Parameter.VAR_KEYWORD for item in parameters
            )
        except (TypeError, ValueError):
            accepts_stats = False
        if accepts_stats:
            self.progress_callback(
                label,
                done,
                total,
                cache_hits=report.get("cache_pairs", 0),
                remote_pairs=report.get("live_pairs", 0),
                inflight_pairs=self._inflight_routes,
            )
        else:
            self.progress_callback(label, done, total)

    @staticmethod
    def _fallback(
        reason: str,
        fallback: np.ndarray | None,
        report: dict[str, Any],
        allow_fallback: bool,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        if not allow_fallback or fallback is None:
            raise RuntimeError(reason)
        log.warning("routing backend unavailable; using fallback matrix: %s", reason)
        return fallback, {**report, "reason": reason}

    def build(
        self,
        demand: gpd.GeoDataFrame,
        supply: gpd.GeoDataFrame,
        eligible_mask: np.ndarray,
        *,
        fallback_matrix: np.ndarray | None = None,
        allow_fallback: bool = True,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        """Build, checkpoint, validate, and cache one travel-time matrix."""
        mask = np.asarray(eligible_mask, dtype=bool)
        expected = (len(demand), len(supply))
        if mask.shape != expected:
            raise ValueError(f"eligible_mask shape {mask.shape} does not match {expected}")
        fallback = (
            np.asarray(fallback_matrix, dtype=float)
            if fallback_matrix is not None
            else None
        )
        if fallback is not None:
            validate_travel_matrix(fallback, *expected)
        self._inflight_routes = 0
        provider_label = self.backend.identity.provider.upper()
        report = {
            "router": "haversine",
            "tier": "fallback",
            "total_pairs": int(mask.size),
            "eligible_pairs": int(mask.sum()),
            "pruned_pairs": int((~mask).sum()),
            "cache_pairs": 0,
            "live_pairs": 0,
            "planned_requests": 0,
            "completed_requests": 0,
            "completed_routes": 0,
            "matrix_cache_hit": False,
        }
        demand_coords = point_coordinates(demand, "demand")
        supply_coords = point_coordinates(supply, "supply")
        dkeys = [
            tuple(round(value, self.coordinate_precision) for value in coordinates)
            for coordinates in demand_coords
        ]
        skeys = [
            tuple(round(value, self.coordinate_precision) for value in coordinates)
            for coordinates in supply_coords
        ]
        matrix = np.full(expected, np.inf)
        missing = mask.copy()
        if not mask.any():
            report.update(router=f"{self.backend.identity.provider}_sparse", tier="pruned")
            return matrix, report

        try:
            if self.cancel_check:
                self.cancel_check()
            matrix_key = build_matrix_key(
                self.backend.identity,
                dkeys,
                skeys,
                mask,
                self.coordinate_precision,
            )
            complete = self.cache.load_matrix(matrix_key, expected)
            if complete is not None:
                report.update(
                    router=f"{self.backend.identity.provider}_sparse",
                    tier="cache",
                    cache_level="matrix",
                    cache_pairs=int(mask.sum()),
                    matrix_cache_hit=True,
                )
                return complete, report

            pair_requests = [
                PairRequest(
                    int(row),
                    int(column),
                    build_pair_key(
                        self.backend.identity,
                        dkeys[int(row)],
                        skeys[int(column)],
                    ),
                )
                for row, column in zip(*np.nonzero(mask))
            ]
            total_pairs = len(pair_requests)
            self._progress(f"{provider_label} cache scan", 0, total_pairs, report)
            for row, column, minutes in self.cache.lookup_pairs(
                pair_requests,
                cancel_check=self.cancel_check,
            ):
                matrix[row, column] = minutes
                missing[row, column] = False
            report["cache_pairs"] = int(mask.sum() - missing.sum())
            self._progress(
                f"{provider_label} cache scan", total_pairs, total_pairs, report
            )

            def save_complete_matrix() -> None:
                try:
                    self.cache.save_matrix(
                        matrix_key,
                        matrix,
                        mask,
                        {
                            "identity": self.backend.identity.canonical(),
                            "coordinate_precision": self.coordinate_precision,
                            "report": report,
                        },
                    )
                except OSError as exc:
                    log.warning("could not persist complete routing matrix: %s", exc)

            if not missing.any():
                report.update(
                    router=f"{self.backend.identity.provider}_sparse",
                    tier="cache",
                    cache_level="pair",
                )
                save_complete_matrix()
                return matrix, report

            block_route_limit = min(
                self.max_routes_per_request,
                self.max_inflight_routes,
            )
            blocks = plan_sparse_blocks(
                demand,
                missing,
                self.source_chunk_size,
                self.tile_size_m,
                block_route_limit,
                self.destination_chunk_size,
            )
            report["planned_requests"] = len(blocks)
            report["planned_routes"] = sum(
                len(sources) * len(destinations)
                for sources, destinations in blocks
            )
            report["max_concurrency"] = self.max_concurrency
            report["max_inflight_routes"] = self.max_inflight_routes
            if len(blocks) > self.max_requests:
                return self._fallback(
                    f"planned requests {len(blocks)} exceed guard {self.max_requests}",
                    fallback,
                    report,
                    allow_fallback,
                )

            self._progress(f"{provider_label} requests", 0, len(blocks), report)
            futures = {}
            next_block = 0
            completed = 0
            cancellation: RunCancelled | None = None
            with ThreadPoolExecutor(
                max_workers=self.max_concurrency,
                thread_name_prefix=self.backend.identity.provider,
            ) as executor:
                while futures or (
                    cancellation is None and next_block < len(blocks)
                ):
                    if cancellation is None and self.cancel_check:
                        try:
                            self.cancel_check()
                        except RunCancelled as exc:
                            cancellation = exc

                    while (
                        cancellation is None
                        and next_block < len(blocks)
                        and len(futures) < self.max_concurrency
                    ):
                        sources, destinations = blocks[next_block]
                        route_count = len(sources) * len(destinations)
                        if route_count > self.max_inflight_routes:
                            raise RuntimeError(
                                f"routing block has {route_count} routes, exceeding "
                                f"inflight budget {self.max_inflight_routes}"
                            )
                        if self._inflight_routes + route_count > self.max_inflight_routes:
                            break
                        future = executor.submit(
                            self.backend.fetch_block,
                            [demand_coords[index] for index in sources],
                            [supply_coords[index] for index in destinations],
                            cancel_check=self.cancel_check,
                        )
                        futures[future] = (
                            next_block + 1,
                            sources,
                            destinations,
                            route_count,
                        )
                        self._inflight_routes += route_count
                        next_block += 1

                    if not futures:
                        break
                    done, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
                    for future in done:
                        block_number, sources, destinations, route_count = futures.pop(future)
                        self._inflight_routes -= route_count
                        try:
                            values = future.result()
                        except RunCancelled as exc:
                            cancellation = cancellation or exc
                            continue

                        pair_values = [
                            (
                                build_pair_key(
                                    self.backend.identity,
                                    dkeys[int(row)],
                                    skeys[int(column)],
                                ),
                                float(values[source_offset, destination_offset]),
                            )
                            for source_offset, row in enumerate(sources)
                            for destination_offset, column in enumerate(destinations)
                        ]
                        # Commit first: cancellation must never discard completed routes.
                        self.cache.store_pairs(pair_values)
                        for source_offset, row in enumerate(sources):
                            for destination_offset, column in enumerate(destinations):
                                if mask[row, column] and missing[row, column]:
                                    matrix[row, column] = float(
                                        values[source_offset, destination_offset]
                                    )
                                    missing[row, column] = False
                                    report["live_pairs"] += 1
                        completed += 1
                        report["completed_requests"] = completed
                        report["completed_routes"] += route_count
                        if (block_number - 1) % 10 == 0:
                            log.info(
                                "routing block %d/%d",
                                block_number,
                                len(blocks),
                            )
                        self._progress(
                            f"{provider_label} requests", completed, len(blocks), report
                        )

            if cancellation is not None:
                raise cancellation
            if missing.any():
                return self._fallback(
                    f"{int(missing.sum())} eligible pairs remain unverified",
                    fallback,
                    report,
                    allow_fallback,
                )
            report.update(
                router=f"{self.backend.identity.provider}_sparse",
                tier="live+cache" if report["cache_pairs"] else "live",
                cache_level="pair+live" if report["cache_pairs"] else "live",
            )
            validate_travel_matrix(matrix, *expected)
            save_complete_matrix()
            return matrix, report
        except RunCancelled:
            raise
        except Exception as exc:
            return self._fallback(str(exc), fallback, report, allow_fallback)

def sparse_network_matrix(
    demand: gpd.GeoDataFrame,
    supply: gpd.GeoDataFrame,
    eligible_mask: np.ndarray,
    *,
    fallback_matrix: np.ndarray | None = None,
    mode: str = "walking",
    api_key: str | None = None,
    base_url: str = ORS_BASE_URL,
    source_chunk_size: int = 25,
    destination_chunk_size: int | None = None,
    tile_size_m: float = 3000,
    max_routes_per_request: int = 2500,
    max_requests: int = 500,
    max_concurrency: int = 2,
    max_inflight_routes: int = 3500,
    request_interval_s: float = 1.6,
    coordinate_precision: int = 5,
    cache_version: str = "v1",
    cache_ttl_days: float | None = None,
    cache_max_gb: float | None = None,
    timeout_s: float = 60,
    progress_callback=None,
    cancel_check: Callable[[], None] | None = None,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Build one sparse, cached, cancellable ORS matrix through injected layers."""
    mask = np.asarray(eligible_mask, dtype=bool)
    expected = (len(demand), len(supply))
    if mask.shape != expected:
        raise ValueError(f"eligible_mask shape {mask.shape} does not match {expected}")
    fallback = (
        fallback_matrix
        if fallback_matrix is not None
        else haversine_matrix(demand, supply)
    )
    validate_travel_matrix(fallback, *expected)
    if coordinate_precision < 0 or request_interval_s < 0:
        raise ValueError("coordinate_precision and request_interval_s must be non-negative")
    if not 1 <= max_concurrency <= 2:
        raise ValueError("max_concurrency must be between 1 and 2")
    if not 1 <= max_routes_per_request <= 3500:
        raise ValueError("max_routes_per_request must be between 1 and 3500")
    if not 1 <= max_inflight_routes <= 3500:
        raise ValueError("max_inflight_routes must be between 1 and 3500")
    try:
        backend = OrsBackend(
            mode=mode,
            api_key=api_key or os.getenv("ORS_API_KEY", ""),
            base_url=base_url,
            timeout_s=timeout_s,
            request_interval_s=request_interval_s,
            cache_version=cache_version,
        )
    except ValueError as exc:
        report = {
            "router": "haversine",
            "tier": "fallback",
            "total_pairs": int(mask.size),
            "eligible_pairs": int(mask.sum()),
            "pruned_pairs": int((~mask).sum()),
            "cache_pairs": 0,
            "live_pairs": 0,
            "planned_requests": 0,
            "completed_requests": 0,
            "completed_routes": 0,
            "matrix_cache_hit": False,
            "reason": str(exc),
        }
        return np.asarray(fallback, dtype=float), report

    cache = CacheStore(
        cache_dir("ors"),
        ttl_days=cache_ttl_days,
        max_size_gb=cache_max_gb,
    )
    try:
        builder = TravelTimeMatrixBuilder(
            backend,
            cache,
            source_chunk_size=source_chunk_size,
            destination_chunk_size=destination_chunk_size,
            tile_size_m=tile_size_m,
            max_routes_per_request=max_routes_per_request,
            max_requests=max_requests,
            max_concurrency=max_concurrency,
            max_inflight_routes=max_inflight_routes,
            coordinate_precision=coordinate_precision,
            progress_callback=progress_callback,
            cancel_check=cancel_check,
        )
        return builder.build(
            demand,
            supply,
            mask,
            fallback_matrix=fallback,
            allow_fallback=True,
        )
    finally:
        cache.close()

