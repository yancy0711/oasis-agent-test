"""Geometry and CRS hygiene -- the most common source of silent errors.

Project-wide convention:
  * store/pass everything in EPSG:4326;
  * project to a local UTM zone ONLY for length/area/centroid math, then back.
Violating this yields silently wrong numbers (degrees, not meters).
"""
from __future__ import annotations
import geopandas as gpd
from pyproj import CRS

STORAGE_CRS = "EPSG:4326"


def ensure_crs(gdf: gpd.GeoDataFrame, target: str = STORAGE_CRS) -> gpd.GeoDataFrame:
    """Single entry point: assume 4326 if missing (warning recorded for the
    report), otherwise reproject."""
    if gdf.crs is None:
        gdf = gdf.set_crs(STORAGE_CRS, allow_override=True)
        gdf.attrs["crs_warning"] = "Source lacked CRS; assumed EPSG:4326"
    if CRS.from_user_input(gdf.crs) != CRS.from_user_input(target):
        gdf = gdf.to_crs(target)
    return gdf


def pick_metric_crs(gdf: gpd.GeoDataFrame) -> str:
    """Auto-select the UTM zone from the study-area centroid.
    This keeps the system city-agnostic -- no hardcoded projections."""
    c = gdf.to_crs(STORAGE_CRS).geometry.union_all().centroid
    zone = int((c.x + 180) // 6) + 1
    return f"EPSG:{(326 if c.y >= 0 else 327)}{zone:02d}"


def repair_geometry(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Drop empty geometries; fix self-intersections via buffer(0)."""
    gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notna()].copy()
    bad = ~gdf.geometry.is_valid
    if bad.any():
        gdf.loc[bad, "geometry"] = gdf.loc[bad, "geometry"].buffer(0)
    return gdf


def centroids(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Centroids computed in metric CRS, returned in 4326."""
    out = gdf.to_crs(pick_metric_crs(gdf)).copy()
    out["geometry"] = out.geometry.centroid
    return out.to_crs(STORAGE_CRS)
