"""Pure distance and travel-matrix validation helpers."""
from __future__ import annotations

from typing import Any

import geopandas as gpd
import numpy as np
from pyproj import CRS

EARTH_R_KM = 6371.0


def point_coordinates(gdf: gpd.GeoDataFrame, name: str) -> list[list[float]]:
    """Validate point geometry and return longitude/latitude pairs."""
    if gdf is None or gdf.empty:
        raise ValueError(f"{name} must contain at least one point")
    if gdf.crs is None or CRS.from_user_input(gdf.crs) != CRS.from_epsg(4326):
        raise ValueError(f"{name} must use EPSG:4326")
    if gdf.geometry.isna().any() or gdf.geometry.is_empty.any():
        raise ValueError(f"{name} contains missing or empty geometry")
    if not gdf.geometry.geom_type.eq("Point").all():
        raise ValueError(f"{name} must contain Point geometries only")
    return np.c_[gdf.geometry.x, gdf.geometry.y].astype(float).tolist()


def haversine_matrix(
    demand: gpd.GeoDataFrame,
    supply: gpd.GeoDataFrame,
    speed_kmh: float = 4.8,
    detour_factor: float = 1.3,
) -> np.ndarray:
    """Return straight-line travel minutes for preview and declared fallback."""
    if speed_kmh <= 0 or detour_factor <= 0:
        raise ValueError("speed_kmh and detour_factor must be positive")
    demand_xy = np.asarray(point_coordinates(demand, "demand"))
    supply_xy = np.asarray(point_coordinates(supply, "supply"))
    source = np.radians(demand_xy[:, [1, 0]])
    destination = np.radians(supply_xy[:, [1, 0]])
    dlat = source[:, None, 0] - destination[None, :, 0]
    dlon = source[:, None, 1] - destination[None, :, 1]
    value = (
        np.sin(dlat / 2) ** 2
        + np.cos(source[:, None, 0])
        * np.cos(destination[None, :, 0])
        * np.sin(dlon / 2) ** 2
    )
    kilometres = 2 * EARTH_R_KM * np.arcsin(np.sqrt(np.clip(value, 0.0, 1.0)))
    return kilometres * detour_factor / speed_kmh * 60.0


def validate_travel_matrix(
    matrix: np.ndarray,
    n_demand: int,
    n_supply: int,
) -> dict[str, Any]:
    """Validate shape and values, then return compact diagnostics."""
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
    finite_values = values[finite]
    return {
        "shape": values.shape,
        "finite_pair_ratio": float(finite.mean()),
        "unreachable_demand_count": int((~reachable_rows).sum()),
        "min_minutes": float(finite_values.min()),
        "max_minutes": float(finite_values.max()),
        "nearest_median_minutes": float(np.median(nearest[reachable_rows])),
        "nearest_p95_minutes": float(np.quantile(nearest[reachable_rows], 0.95)),
    }
