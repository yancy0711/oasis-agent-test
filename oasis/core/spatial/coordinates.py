"""Normalize explicitly declared upload coordinates to WGS84."""
from __future__ import annotations

import geopandas as gpd
import numpy as np
from shapely.ops import transform


def _gcj_offset(lon, lat):
    """Approximate the GCJ-02 offset in degrees at a WGS84 coordinate."""
    a = 6378245.0
    eccentricity_squared = 0.006693421622965943
    x, y = lon - 105, lat - 35
    wave = (20 * np.sin(6 * x * np.pi) + 20 * np.sin(2 * x * np.pi)) * 2 / 3
    dy = -100 + 2 * x + 3 * y + .2 * y**2 + .1 * x * y + .2 * np.sqrt(np.abs(x)) + wave
    dy += ((20 * np.sin(y * np.pi) + 40 * np.sin(y * np.pi / 3)) +
           (160 * np.sin(y * np.pi / 12) + 320 * np.sin(y * np.pi / 30))) * 2 / 3
    dx = 300 + x + 2 * y + .1 * x**2 + .1 * x * y + .1 * np.sqrt(np.abs(x)) + wave
    dx += ((20 * np.sin(x * np.pi) + 40 * np.sin(x * np.pi / 3)) +
           (150 * np.sin(x * np.pi / 12) + 300 * np.sin(x * np.pi / 30))) * 2 / 3
    radians = np.deg2rad(lat)
    scale = 1 - eccentricity_squared * np.sin(radians)**2
    return (np.rad2deg(dx * np.sqrt(scale) / (a * np.cos(radians))),
            np.rad2deg(dy * scale**1.5 / (a * (1 - eccentricity_squared))))


def gcj02_to_wgs84(lon, lat, z=None):
    """Invert the approximate GCJ-02 model using the declared source coordinates."""
    longitude, latitude = np.asarray(lon, dtype=float), np.asarray(lat, dtype=float)
    in_range = (72 <= longitude) & (longitude <= 138) & (1 <= latitude) & (latitude <= 56)
    if not np.all(in_range):
        raise ValueError("GCJ-02 expects mainland China longitude/latitude coordinates.")
    x, y = longitude, latitude
    for _ in range(6):
        dx, dy = _gcj_offset(x, y)
        x, y = longitude - dx, latitude - dy
    return (x, y) if z is None else (x, y, z)


def normalize_wgs84(frame: gpd.GeoDataFrame, coordinate_system: str, epsg: int) -> gpd.GeoDataFrame:
    if coordinate_system == "gcj02":
        frame = frame.copy()
        frame.geometry = frame.geometry.map(lambda geometry: transform(gcj02_to_wgs84, geometry))
        frame = frame.set_crs(4326, allow_override=True)
    elif coordinate_system == "epsg":
        frame = frame.set_crs(epsg).to_crs(4326)
    else:
        raise ValueError("Choose an EPSG code or explicitly declare GCJ-02.")
    bounds = frame.geometry.bounds.to_numpy()
    west, south, east, north = frame.total_bounds
    if (not np.isfinite(bounds).all() or
            not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90)):
        raise ValueError("Coordinates are outside WGS84 bounds. Check the input CRS and longitude/latitude order.")
    return frame
