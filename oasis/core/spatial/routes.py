"""Cached, user-initiated ORS point-to-site route planning."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Callable

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
from shapely.geometry import LineString, shape

from oasis.core.cache import atomic_write_text, cache_dir
from oasis.core.retry import RETRYABLE_STATUS, TransientError, with_retry
from oasis.core.spatial.accessibility.backend import ORS_PROFILES, RequestGate
from oasis.core.spatial.alignment import ensure_crs, pick_metric_crs
from oasis.core.spatial.accessibility.backend import post_ors


def _directions_url(matrix_url: str, profile: str) -> str:
    root = (matrix_url or "https://api.heigit.org/openrouteservice/v2/matrix").rstrip("/")
    if root.endswith("/matrix"):
        root = root[:-7] + "/directions"
    return f"{root}/{profile}/geojson"


@with_retry(max_retries=3)
def _request_route(
    url: str,
    api_key: str,
    origin: list[float],
    destination: list[float],
    timeout_s: float,
    gate: RequestGate,
    cancel_check: Callable[[], None] | None,
) -> dict:
    gate.wait(cancel_check)
    try:
        response = requests.post(
            url,
            headers={"Authorization": api_key, "Accept": "application/geo+json",
                     "Content-Type": "application/json"},
            json={"coordinates": [origin, destination]},
            timeout=timeout_s,
        )
    except requests.RequestException as exc:
        raise TransientError(f"ORS directions request failed: {exc}") from exc
    if response.status_code in RETRYABLE_STATUS:
        raise TransientError(f"ORS directions returned HTTP {response.status_code}")
    if not response.ok:
        raise RuntimeError(f"ORS directions returned HTTP {response.status_code}: {response.text[:200]}")
    payload = response.json()
    if not payload.get("features"):
        raise RuntimeError("ORS directions response contains no route feature")
    return payload["features"][0]


def _cached_route(
    origin: list[float],
    destination: list[float],
    *,
    mode: str,
    base_url: str,
    api_key: str,
    timeout_s: float,
    gate: RequestGate,
    cancel_check: Callable[[], None] | None,
) -> tuple[dict, str]:
    base_url = base_url or "https://api.heigit.org/openrouteservice/v2/matrix"
    profile = ORS_PROFILES.get(mode)
    if profile is None:
        raise ValueError(f"unsupported ORS directions mode: {mode}")
    identity = json.dumps({
        "origin": [round(value, 6) for value in origin],
        "destination": [round(value, 6) for value in destination],
        "profile": profile,
        "version": "v1",
    }, sort_keys=True)
    path = cache_dir("ors_routes") / f"route_{hashlib.sha256(identity.encode()).hexdigest()[:24]}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8")), "cache"
    feature = _request_route(
        _directions_url(base_url, profile), api_key, origin, destination,
        timeout_s, gate, cancel_check,
    )
    atomic_write_text(path, json.dumps(feature))
    return feature, "live"


def _length_weighted_environment(line, areas: gpd.GeoDataFrame) -> dict[str, float | None]:
    metric_crs = pick_metric_crs(areas)
    projected = areas.to_crs(metric_crs)
    route = gpd.GeoSeries([line], crs="EPSG:4326").to_crs(metric_crs).iloc[0]
    candidates = projected.loc[projected.intersects(route)].copy()
    if candidates.empty:
        return {name: None for name in ("route_mean_lst_c", "route_mean_ndvi", "route_mean_ndbi", "route_mean_albedo")}
    lengths = candidates.geometry.intersection(route).length.to_numpy(float)
    mapping = {
        "route_mean_lst_c": "mean_lst",
        "route_mean_ndvi": "mean_ndvi",
        "route_mean_ndbi": "mean_ndbi",
        "route_mean_albedo": "mean_albedo",
    }
    result = {}
    for output, source in mapping.items():
        if source not in candidates:
            result[output] = None
            continue
        values = pd.to_numeric(candidates.get(source), errors="coerce").to_numpy(float)
        valid = np.isfinite(values) & (lengths > 0)
        result[output] = float(np.average(values[valid], weights=lengths[valid])) if valid.any() else None
    return result


def plan_point_to_nearest_site(
    longitude: float,
    latitude: float,
    areas: gpd.GeoDataFrame,
    candidates: gpd.GeoDataFrame,
    selected_ids: list[str],
    *,
    mode: str,
    base_url: str = "https://api.heigit.org/openrouteservice/v2/matrix",
    timeout_s: float = 60,
    request_interval_s: float = 1.6,
    candidate_limit: int = 15,
    study_boundary=None,
    cancel_check: Callable[[], None] | None = None,
) -> dict:
    """Shortlist nearby selected facilities, then find the network-nearest route."""
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        raise ValueError("route origin must be valid longitude/latitude")
    areas, candidates = ensure_crs(areas), ensure_crs(candidates)
    selected = candidates[candidates["cand_id"].astype(str).isin(set(map(str, selected_ids)))].copy()
    if selected.empty:
        raise ValueError("the completed solution contains no selected candidate facilities")
    origin_point = gpd.points_from_xy([longitude], [latitude], crs="EPSG:4326")[0]
    tract = areas.loc[areas.geometry.covers(origin_point)]
    coverage = study_boundary if study_boundary is not None else areas.geometry.union_all()
    if not coverage.covers(origin_point):
        raise ValueError(
            "This origin is outside the current study area; select a point "
            "inside the displayed study boundary."
        )
    api_key = os.getenv("ORS_API_KEY", "")
    if not api_key:
        raise ValueError("ORS_API_KEY is not set for point route planning")
    profile = ORS_PROFILES.get(mode)
    if profile is None:
        raise ValueError(f"unsupported route-planning mode: {mode}")
    gate = RequestGate(request_interval_s)
    origin = [float(longitude), float(latitude)]
    pool_size = len(selected)
    if pool_size > candidate_limit:
        coordinates = np.column_stack((selected.geometry.x, selected.geometry.y))
        longitude_scale = np.cos(np.radians(latitude))
        approximate_distance = np.hypot(
            (coordinates[:, 0] - longitude) * longitude_scale,
            coordinates[:, 1] - latitude,
        )
        selected = selected.iloc[np.argsort(approximate_distance)[:candidate_limit]].copy()
    destinations = [[float(point.x), float(point.y)] for point in selected.geometry]
    matrix = post_ors(
        f"{base_url.rstrip('/')}/{profile}", api_key,
        {"locations": [origin, *destinations], "sources": ["0"],
         "destinations": [str(index) for index in range(1, len(destinations) + 1)],
         "metrics": ["duration"]},
        timeout_s, gate=gate, cancel_check=cancel_check,
    )
    durations = np.asarray(matrix["durations"][0], dtype=float)
    if not np.isfinite(durations).any():
        raise ValueError("none of the selected candidate facilities is reachable from this point")
    site_index = int(np.nanargmin(durations))
    destination = destinations[site_index]
    feature, tier = _cached_route(
        origin, destination, mode=mode, base_url=base_url, api_key=api_key,
        timeout_s=timeout_s, gate=gate, cancel_check=cancel_check,
    )
    geometry = shape(feature["geometry"])
    if not isinstance(geometry, LineString):
        raise ValueError("ORS route geometry is not a LineString")
    route_summary = feature.get("properties", {}).get("summary", {})
    environment = _length_weighted_environment(geometry, areas)
    properties = {
        "layer_kind": "planned_route", "origin_source": "user_selected_point",
        "origin_geoid": str(tract.iloc[0]["GEOID"]) if not tract.empty else None,
        "destination_id": str(selected.iloc[site_index]["cand_id"]),
        "destination_name": str(selected.iloc[site_index].get("name") or selected.iloc[site_index]["cand_id"]),
        "mode": mode,
        "duration_minutes": float(route_summary.get("duration", durations[site_index])) / 60,
        "distance_km": float(route_summary.get("distance", 0)) / 1000,
        "geometry_source": "ORS directions GeoJSON", "cache_tier": tier,
        "selection_method": (
            "minimum ORS network travel time among the nearest selected candidate facilities"
        ),
        "selected_facility_pool": pool_size,
        "ors_shortlist_size": len(selected),
        **environment,
    }
    properties["surface_temperature_minutes"] = (
        properties["route_mean_lst_c"] * properties["duration_minutes"]
        if properties["route_mean_lst_c"] is not None else None
    )
    properties["active_heat_audit"] = {
        "status": "available" if properties["route_mean_lst_c"] is not None else "environment_unavailable",
        "exposure_celsius_minutes": properties["surface_temperature_minutes"],
        "method": "tract-level route-intersection length weighting",
        "interpretation": "post-route audit only; it does not alter ORS routing or facility optimization",
        "spatial_resolution": "tract-level environment overlay, not street-segment heat",
    }
    feature["properties"] = properties
    return {"type": "FeatureCollection", "features": [feature], "summary": properties}
