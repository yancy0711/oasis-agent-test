"""Travel-time matrix, layered implementation.

v1 (haversine) exists so the optimizer can be developed and tested before any
routing service is wired up -- it decouples the two workstreams. It then
stays on as the runtime fallback; the router actually used goes into
Provenance so degradation is transparent.
"""
from __future__ import annotations
import hashlib
import json
import logging
import math
import os
import sqlite3
import time
from collections import defaultdict
from typing import Any

import numpy as np
import geopandas as gpd
import requests
from pyproj import CRS

from oasis.core.cache import cache_dir
from oasis.core.retry import RETRYABLE_STATUS, TransientError, with_retry
from oasis.core.spatial.alignment import pick_metric_crs

EARTH_R_KM = 6371.0
ORS_BASE_URL = "https://api.heigit.org/openrouteservice/v2/matrix"
ORS_PROFILES = {
    "walking": "foot-walking",
    "foot-walking": "foot-walking",
    "cycling": "cycling-regular",
    "cycling-regular": "cycling-regular",
    "driving": "driving-car",
    "driving-car": "driving-car",
    "wheelchair": "wheelchair",
}
log = logging.getLogger(__name__)


def _point_coordinates(gdf: gpd.GeoDataFrame, name: str) -> list[list[float]]:
    """Validate point geometry and return longitude/latitude coordinate pairs."""
    if gdf is None or gdf.empty:
        raise ValueError(f"{name} must contain at least one point")
    if gdf.crs is None or CRS.from_user_input(gdf.crs) != CRS.from_epsg(4326):
        raise ValueError(f"{name} must use EPSG:4326")
    if gdf.geometry.isna().any() or gdf.geometry.is_empty.any():
        raise ValueError(f"{name} contains missing or empty geometry")
    if not gdf.geometry.geom_type.eq("Point").all():
        raise ValueError(f"{name} must contain Point geometries only")
    return np.c_[gdf.geometry.x, gdf.geometry.y].astype(float).tolist()


def haversine_matrix(demand: gpd.GeoDataFrame, supply: gpd.GeoDataFrame,
                     speed_kmh: float = 4.8, detour_factor: float = 1.3) -> np.ndarray:
    """v1 / fallback: straight-line distance x detour factor / walking speed."""
    if speed_kmh <= 0 or detour_factor <= 0:
        raise ValueError("speed_kmh and detour_factor must be positive")
    demand_xy = np.asarray(_point_coordinates(demand, "demand"))
    supply_xy = np.asarray(_point_coordinates(supply, "supply"))
    d = np.radians(demand_xy[:, [1, 0]])
    s = np.radians(supply_xy[:, [1, 0]])
    dlat = d[:, None, 0] - s[None, :, 0]
    dlon = d[:, None, 1] - s[None, :, 1]
    a = np.sin(dlat / 2) ** 2 + np.cos(d[:, None, 0]) * np.cos(s[None, :, 0]) * np.sin(dlon / 2) ** 2
    km = 2 * EARTH_R_KM * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))
    return km * detour_factor / speed_kmh * 60.0


def validate_travel_matrix(
    matrix: np.ndarray,
    n_demand: int,
    n_supply: int,
) -> dict[str, Any]:
    """Validate shape and values, then return compact diagnostics.

    Positive infinity represents an unroutable ORS pair. NaN and negative
    travel times are always invalid.
    """
    values = np.asarray(matrix, dtype=float)
    expected = (n_demand, n_supply)
    if values.ndim != 2 or values.shape != expected:
        raise ValueError(
            f"travel matrix shape {values.shape} does not match {expected}"
        )
    if np.isnan(values).any():
        raise ValueError("travel matrix contains NaN")
    if (values < 0).any():
        raise ValueError("travel matrix contains negative travel times")

    finite = np.isfinite(values)
    if not finite.any():
        raise ValueError("travel matrix contains no reachable demand-supply pair")

    nearest = values.min(axis=1)
    reachable_rows = np.isfinite(nearest)
    nearest_finite = nearest[reachable_rows]
    finite_values = values[finite]
    return {
        "shape": values.shape,
        "finite_pair_ratio": float(finite.mean()),
        "unreachable_demand_count": int((~reachable_rows).sum()),
        "min_minutes": float(finite_values.min()),
        "max_minutes": float(finite_values.max()),
        "nearest_median_minutes": float(np.median(nearest_finite)),
        "nearest_p95_minutes": float(np.quantile(nearest_finite, 0.95)),
    }


@with_retry(max_retries=4)
def _post_ors(
    url: str,
    api_key: str,
    payload: dict[str, Any],
    timeout_s: float,
) -> dict[str, Any]:
    ''' Post a matrix request to ORS, with retry on transient errors. Raises RuntimeError on non-retryable HTTP errors or malformed responses. '''
    try:
        response = requests.post(
            url,
            headers={
                "Authorization": api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout_s,
        )
    except requests.RequestException as exc:
        raise TransientError(f"ORS request failed: {exc}") from exc

    if response.status_code in RETRYABLE_STATUS:
        raise TransientError(f"ORS returned HTTP {response.status_code}")
    if not response.ok:
        detail = response.text.strip().replace("\n", " ")[:300]
        raise RuntimeError(f"ORS returned HTTP {response.status_code}: {detail}")

    result = response.json()
    if "durations" not in result:
        raise RuntimeError("ORS response contains no durations matrix")
    return result


def _ors_block(
    demand_coords: list[list[float]],
    supply_coords: list[list[float]],
    *,
    profile: str,
    api_key: str,
    base_url: str,
    timeout_s: float,
) -> np.ndarray:
    ''' Return a single ORS duration matrix block in minutes. The block is keyed by a hash of the request payload, so repeated calls with the same coordinates will hit the cache. '''
    locations = demand_coords + supply_coords
    n_demand = len(demand_coords)
    payload = {
        "locations": locations,
        "sources": [str(i) for i in range(n_demand)],
        "destinations": [str(i) for i in range(n_demand, len(locations))],
        "metrics": ["duration"],
    }
    url = f"{base_url.rstrip('/')}/{profile}"
    digest = hashlib.sha256(
        json.dumps(
            {"url": url, "payload": payload},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:20]
    path = cache_dir("ors") / f"matrix_{digest}.json"

    if path.exists():
        result = json.loads(path.read_text(encoding="utf-8"))
    else:
        result = _post_ors(url, api_key, payload, timeout_s)
        path.write_text(
            json.dumps(result, separators=(",", ":")),
            encoding="utf-8",
        )

    durations = np.asarray(
        [
            [np.inf if value is None else float(value) / 60.0 for value in row]
            for row in result["durations"]
        ],
        dtype=float,
    )
    expected = (len(demand_coords), len(supply_coords))
    if durations.shape != expected:
        raise RuntimeError(
            f"ORS block shape {durations.shape} does not match {expected}"
        )
    return durations


def network_matrix(
    demand: gpd.GeoDataFrame,
    supply: gpd.GeoDataFrame,
    mode: str = "walking",
    *,
    api_key: str | None = None,
    base_url: str = ORS_BASE_URL,
    source_chunk_size: int = 50,
    destination_chunk_size: int = 50,
    max_routes_per_request: int = 2500,
    max_requests: int | None = None,
    timeout_s: float = 60.0,
) -> np.ndarray:
    """Return a blockwise ORS duration matrix in minutes.

    max_requests is a quota guard. A full county matrix can require thousands
    of public API calls, so callers must make that cost explicit.
    """
    demand_coords = _point_coordinates(demand, "demand")
    supply_coords = _point_coordinates(supply, "supply")
    profile = ORS_PROFILES.get(mode)
    if profile is None:
        raise ValueError(f"unsupported ORS mode/profile: {mode}")

    key = api_key or os.getenv("ORS_API_KEY", "")
    if not key:
        raise ValueError("ORS_API_KEY is not set")
    if source_chunk_size <= 0 or destination_chunk_size <= 0:
        raise ValueError("ORS chunk sizes must be positive")
    if max_routes_per_request <= 0:
        raise ValueError("max_routes_per_request must be positive")

    source_chunk_size = min(source_chunk_size, max_routes_per_request)
    destination_chunk_size = min(
        destination_chunk_size,
        max(1, max_routes_per_request // source_chunk_size),
    )
    request_count = math.ceil(len(demand_coords) / source_chunk_size) * math.ceil(
        len(supply_coords) / destination_chunk_size
    )
    if max_requests is not None and request_count > max_requests:
        raise RuntimeError(
            f"ORS matrix requires {request_count} requests, exceeding "
            f"the configured quota guard ({max_requests})"
        )

    matrix = np.full((len(demand_coords), len(supply_coords)), np.inf)
    for d0 in range(0, len(demand_coords), source_chunk_size):
        d1 = min(d0 + source_chunk_size, len(demand_coords))
        for s0 in range(0, len(supply_coords), destination_chunk_size):
            s1 = min(s0 + destination_chunk_size, len(supply_coords))
            matrix[d0:d1, s0:s1] = _ors_block(
                demand_coords[d0:d1],
                supply_coords[s0:s1],
                profile=profile,
                api_key=key,
                base_url=base_url,
                timeout_s=timeout_s,
            )

    validate_travel_matrix(matrix, len(demand), len(supply))
    return matrix


def plan_sparse_blocks(
    demand: gpd.GeoDataFrame,
    eligible_mask: np.ndarray,
    source_chunk_size: int = 25,
    tile_size_m: float = 3000,
    max_routes_per_request: int = 2500,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Cover sparse pairs with spatially local rectangular ORS blocks."""
    mask = np.asarray(eligible_mask, dtype=bool)
    if mask.ndim != 2 or mask.shape[0] != len(demand):
        raise ValueError("eligible_mask rows must match demand")
    if min(source_chunk_size, tile_size_m, max_routes_per_request) <= 0:
        raise ValueError("sparse block parameters must be positive")

    points = demand.to_crs(pick_metric_crs(demand)).geometry
    x, y = points.x.to_numpy(), points.y.to_numpy()
    tx, ty = np.floor((x - x.min()) / tile_size_m), np.floor((y - y.min()) / tile_size_m)
    tiles: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, tile in enumerate(zip(tx.astype(int), ty.astype(int))):
        if mask[i].any():
            tiles[tile].append(i)

    blocks = []
    for indices in tiles.values():
        for start in range(0, len(indices), source_chunk_size):
            sources = np.asarray(indices[start:start + source_chunk_size], dtype=int)
            destinations = np.flatnonzero(mask[sources].any(axis=0))
            size = max(1, max_routes_per_request // len(sources))
            blocks.extend((sources, destinations[j:j + size]) for j in range(0, len(destinations), size))
    return blocks


def _print_progress(label: str, done: int, total: int) -> None:
    """Print one in-place progress bar for cache scans and live ORS requests."""
    total = max(total, 1); width = 24
    filled = min(width, int(width * done / total))
    bar = "#" * filled + "-" * (width - filled)
    print(f"\r{label} [{bar}] {done}/{total}", end="\n" if done >= total else "", flush=True)



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
    tile_size_m: float = 3000,
    max_routes_per_request: int = 2500,
    max_requests: int = 500,
    request_interval_s: float = 1.6,
    coordinate_precision: int = 5,
    cache_version: str = "v1",
    timeout_s: float = 60,
    progress_callback=None,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Verify eligible pairs with ORS; incomplete runs fall back as a whole."""
    mask = np.asarray(eligible_mask, dtype=bool)
    expected = (len(demand), len(supply))
    if mask.shape != expected:
        raise ValueError(f"eligible_mask shape {mask.shape} does not match {expected}")
    if coordinate_precision < 0 or request_interval_s < 0:
        raise ValueError("coordinate_precision and request_interval_s must be non-negative")

    fallback = fallback_matrix if fallback_matrix is not None else haversine_matrix(demand, supply)
    validate_travel_matrix(fallback, *expected)
    report = {
        "router": "haversine", "tier": "fallback", "total_pairs": int(mask.size),
        "eligible_pairs": int(mask.sum()), "pruned_pairs": int((~mask).sum()),
        "cache_pairs": 0, "live_pairs": 0, "planned_requests": 0,
    }

    def report_progress(label: str, done: int, total: int) -> None:
        """Report real cache/request counts to terminal and an optional UI callback."""
        _print_progress(label, done, total)
        if progress_callback: progress_callback(label, done, total)

    def fail(reason: str):
        """Return the complete Haversine fallback with a recorded reason."""
        log.warning("sparse ORS unavailable; using full Haversine matrix: %s", reason)
        return fallback, {**report, "reason": reason}

    profile = ORS_PROFILES.get(mode)
    if profile is None:
        return fail(f"unsupported ORS mode/profile: {mode}")

    demand_coords = _point_coordinates(demand, "demand")
    supply_coords = _point_coordinates(supply, "supply")
    dkeys = [tuple(round(v, coordinate_precision) for v in xy) for xy in demand_coords]
    skeys = [tuple(round(v, coordinate_precision) for v in xy) for xy in supply_coords]
    matrix = np.full(expected, np.inf)
    missing = mask.copy()
    if not mask.any():
        report.update(router="ors_sparse", tier="pruned")
        return matrix, report

    connection = None
    try:
        connection = sqlite3.connect(cache_dir("ors") / "pair_durations.sqlite")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS durations (version TEXT, profile TEXT, base_url TEXT, "
            "src_lon REAL, src_lat REAL, dst_lon REAL, dst_lat REAL, minutes REAL, "
            "PRIMARY KEY (version, profile, base_url, src_lon, src_lat, dst_lon, dst_lat))"
        )
        cursor = connection.cursor()
        rows, columns = np.nonzero(mask)
        total_pairs, progress_step = len(rows), max(1, len(rows) // 100)
        report_progress("ORS cache scan", 0, total_pairs)
        for number, (i, j) in enumerate(zip(rows, columns), 1):
            row = cursor.execute(
                "SELECT minutes FROM durations WHERE version=? AND profile=? AND base_url=? "
                "AND src_lon=? AND src_lat=? AND dst_lon=? AND dst_lat=?",
                (cache_version, profile, base_url, *dkeys[i], *skeys[j]),
            ).fetchone()
            if row is not None:
                matrix[i, j], missing[i, j] = float(row[0]), False

            if number % progress_step == 0 or number == total_pairs:
                report_progress("ORS cache scan", number, total_pairs)
        report["cache_pairs"] = int(mask.sum() - missing.sum())
        print(f"ORS cache hits {report['cache_pairs']}/{int(mask.sum())}; missing {int(missing.sum())}", flush=True)
        if not missing.any():
            report.update(router="ors_sparse", tier="cache")
            return matrix, report

        blocks = plan_sparse_blocks(demand, missing, source_chunk_size, tile_size_m, max_routes_per_request)
        report["planned_requests"] = len(blocks)
        report["planned_routes"] = sum(len(s) * len(d) for s, d in blocks)
        if len(blocks) > max_requests:
            return fail(f"planned requests {len(blocks)} exceed guard {max_requests}")
        key = api_key or os.getenv("ORS_API_KEY", "")
        if not key:
            return fail("ORS_API_KEY is not set")

        report_progress("ORS requests", 0, len(blocks))
        for number, (sources, destinations) in enumerate(blocks):
            if number % 10 == 0:
                log.info("sparse ORS block %d/%d", number + 1, len(blocks))
            if number and request_interval_s:
                time.sleep(request_interval_s)
            values = _ors_block(
                [demand_coords[i] for i in sources], [supply_coords[j] for j in destinations],
                profile=profile, api_key=key, base_url=base_url, timeout_s=timeout_s,
            )
            records = []
            for a, i in enumerate(sources):
                for b, j in enumerate(destinations):
                    minutes = float(values[a, b])
                    records.append((cache_version, profile, base_url, *dkeys[i], *skeys[j], minutes))
                    if mask[i, j] and missing[i, j]:
                        matrix[i, j], missing[i, j], report["live_pairs"] = minutes, False, report["live_pairs"] + 1
            cursor.executemany("INSERT OR REPLACE INTO durations VALUES (?,?,?,?,?,?,?,?)", records)
            connection.commit()
            report_progress("ORS requests", number + 1, len(blocks))

        if missing.any():
            return fail(f"{int(missing.sum())} eligible pairs remain unverified")
        report.update(router="ors_sparse", tier="live+cache" if report["cache_pairs"] else "live")
        validate_travel_matrix(matrix, *expected)
        return matrix, report
    except Exception as exc:
        return fail(str(exc))
    finally:
        if connection is not None:
            connection.close()


def travel_time_matrix(demand, supply, cfg: dict) -> tuple[np.ndarray, str]:
    """Unified entry. Returns (minutes matrix, router actually used)."""
    router = cfg.get("router", "haversine")
    if router == "ors":
        try:
            matrix = network_matrix(
                demand,
                supply,
                mode=cfg.get("mode", "walking"),
                api_key=cfg.get("api_key"),
                base_url=cfg.get("ors_base_url", ORS_BASE_URL),
                source_chunk_size=cfg.get("ors_source_chunk_size", 50),
                destination_chunk_size=cfg.get("ors_destination_chunk_size", 50),
                max_routes_per_request=cfg.get("ors_max_routes_per_request", 2500),
                max_requests=cfg.get("ors_max_requests", 100),
                timeout_s=cfg.get("ors_timeout_s", 60),
            )
            return matrix, "ors"
        except Exception as exc:
            log.warning("ORS matrix unavailable; using Haversine: %s", exc)
    elif router == "osrm":
        log.warning("OSRM matrix is not implemented; using Haversine")
    elif router != "haversine":
        log.warning("unknown router '%s'; using Haversine", router)

    matrix = haversine_matrix(
        demand,
        supply,
        cfg.get("speed_kmh", 4.8),
        cfg.get("detour_factor", 1.3),
    )
    validate_travel_matrix(matrix, len(demand), len(supply))
    return matrix, "haversine"
