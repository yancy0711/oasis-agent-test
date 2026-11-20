"""Nanshan district boundary and gridded WorldPop demand."""
from __future__ import annotations

import json
from math import ceil, floor, isfinite
from threading import Lock

import geopandas as gpd
import numpy as np
import requests
from shapely import make_valid, union_all
from shapely.geometry import box, mapping, shape

from oasis.core.cache import CACHE_DIR, atomic_write_text

NAME = "Nanshan District, Shenzhen, China"
YEAR = 2022
GRID_M = 3000
BOUNDARY_URL = "https://www.openstreetmap.org/relation/5664195"
SOURCE = "OpenStreetMap boundary + WorldPop Global2 R2025A"
SOURCE_URL = "https://api.worldpop.org/v3/about"
NOTES = f"{BOUNDARY_URL}; {GRID_M / 1000:g} km projected grid clipped to the district; WorldPop 100 m estimated population and age 65+"
_CACHE = f"nanshan_osm_worldpop_r2025a_{YEAR}_{GRID_M}.json"
_LOCK = Lock()
_BOUNDARY_LOCK = Lock()


def _boundary():
    response = requests.get("https://nominatim.openstreetmap.org/lookup", params={
        "osm_ids": "R5664195", "format": "jsonv2", "polygon_geojson": 1,
        "polygon_threshold": 0.00003, "addressdetails": 1,
    }, headers={"User-Agent": "oasis-agent/0.1"}, timeout=(5, 15))
    response.raise_for_status()
    rows = response.json()
    if len(rows) != 1 or str(rows[0].get("osm_id")) != "5664195":
        raise ValueError("The registered Nanshan polygon was unavailable from OpenStreetMap.")
    geometry = make_valid(shape(rows[0]["geojson"]))
    if geometry.is_empty or geometry.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError("The registered Nanshan geometry was not a district polygon.")
    west, south, east, north = geometry.bounds
    if not (113.6 <= west < east <= 114.15 and 22.15 <= south < north <= 22.8):
        raise ValueError("The Nanshan boundary was outside the expected study region.")
    return geometry


def _zones(boundary, grid_m=GRID_M, *, crs=None):
    series = gpd.GeoSeries([boundary], crs=4326)
    crs = crs or series.estimate_utm_crs()
    projected = series.to_crs(crs).iat[0]
    west, south, east, north = projected.bounds
    zones = []
    for x in range(floor(west / grid_m), ceil(east / grid_m)):
        for y in range(floor(south / grid_m), ceil(north / grid_m)):
            clipped = projected.intersection(box(x * grid_m, y * grid_m, (x + 1) * grid_m, (y + 1) * grid_m))
            if clipped.area <= 0:
                continue
            if clipped.geom_type == "GeometryCollection":
                clipped = union_all([part for part in clipped.geoms if part.area > 0])
            zones.append({"GEOID": f"grid-{x}-{y}", "geometry": clipped})
    return gpd.GeoDataFrame(zones, crs=crs).to_crs(4326)


def _population(zones: gpd.GeoDataFrame) -> tuple[list[float], list[float]]:
    features = [{"type": "Feature", "id": unit, "properties": {}, "geometry": mapping(geometry)}
                for unit, geometry in zip(zones["GEOID"], zones.geometry)]
    response = requests.post("https://api.worldpop.org/v3/batch", json={
        "features": {"type": "FeatureCollection", "features": features},
        "year": YEAR, "include": "agesex",
    }, timeout=(5, 35))
    response.raise_for_status()
    data = response.json()
    rows = data.get("features", [])
    by_id = {row.get("id"): row for row in rows}
    if data.get("data_year") != YEAR or len(rows) != len(zones) or set(by_id) != set(zones["GEOID"]):
        raise ValueError("WorldPop did not return all demand zones for the configured year.")
    totals, older = [], []
    for unit in zones["GEOID"]:
        row = by_id[unit]
        total = float(row["total_population"])
        if not isfinite(total) or total < 0:
            raise ValueError("WorldPop returned an invalid population estimate.")
        bands = {int(band["class"]): float(band["female"]) + float(band["male"])
                 for band in row.get("agesex_pyramid", [])}
        elderly = sum(bands[age] for age in (65, 70, 75, 80, 85, 90)) if {65, 70, 75, 80, 85, 90} <= bands.keys() else np.nan
        # WorldPop rounds each sex/age band independently.
        complete = abs(sum(bands.values()) - total) <= max(.25, .01 * total)
        totals.append(total)
        older.append(min(elderly, total) if complete and isfinite(elderly) and 0 <= elderly <= total + .25 else np.nan)
    return totals, older


def nanshan_boundary(*, cached_only: bool = False):
    folder = CACHE_DIR / "regional"
    path = folder / "nanshan_osm_5664195.geojson"
    with _BOUNDARY_LOCK:
        if path.is_file():
            return shape(json.loads(path.read_text(encoding="utf-8")))
        if (folder / _CACHE).is_file():
            return shape(json.loads((folder / _CACHE).read_text(encoding="utf-8"))["boundary"])
        if cached_only:
            return None
        boundary = _boundary()
        atomic_write_text(path, json.dumps(mapping(boundary)))
        return boundary


def nanshan_data() -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Refresh the regional dataset together so a partial estimate cannot enter a run."""
    path = CACHE_DIR / "regional" / _CACHE
    with _LOCK:
        if path.is_file():
            cached = json.loads(path.read_text(encoding="utf-8"))
        else:
            boundary = nanshan_boundary()
            zones = _zones(boundary, crs=32649)
            totals, older = _population(zones)
            zones["total_population"], zones["elderly_population"] = totals, older
            zones = zones.loc[zones["total_population"].gt(0)].copy()
            if zones.empty:
                raise ValueError("WorldPop returned no populated demand zones for Nanshan.")
            cached = {"boundary": mapping(boundary), "zones": json.loads(zones.to_json(drop_id=True))}
            atomic_write_text(path, json.dumps(cached, ensure_ascii=False))
    frame = gpd.GeoDataFrame.from_features(cached["zones"]["features"], crs=4326)
    frame["elderly_population"] = frame["elderly_population"].astype(float)
    return gpd.GeoDataFrame(geometry=[shape(cached["boundary"])], crs=4326), frame
