"""CIESIN Global Gridded Relative Deprivation Index for regional demand zones."""
from __future__ import annotations

import os
from pathlib import Path
from threading import Lock
from typing import Callable

import geopandas as gpd
import numpy as np
import rasterio
import requests
from rasterio.errors import WindowError
from rasterio.features import geometry_window
import shapely

from oasis.core.cache import CACHE_DIR, atomic_target
from oasis.contracts.domain import Provenance

FIELD = "deprivation_index"
SOURCE = "CIESIN / NASA SEDAC Global Gridded Relative Deprivation Index v1 (2022)"
SOURCE_URL = "https://doi.org/10.7927/3xxe-ap97"
DOWNLOAD_URL = (
    "https://data.naturalcapitalalliance.stanford.edu/download/global/"
    "ciesin-nasa-gpw-grdi/ciesin_nasa_povmap-grdi-v1.tif"
)
METHOD = "Mean of valid GRDI pixels weighted by their overlap with the demand zone, 0–100 index points."
_DOWNLOAD_LOCK = Lock()


def _raster_path(cancel: Callable[[], None], progress: Callable[[int, int], None]) -> tuple[Path, str]:
    if local := os.getenv("OASIS_GRDI_PATH"):
        path = Path(local).expanduser()
        if not path.is_file():
            raise ValueError("OASIS_GRDI_PATH does not point to a readable GRDI v1 GeoTIFF.")
        return path, "local"
    path = CACHE_DIR / "regional" / "povmap-grdi-v1.tif"
    while not _DOWNLOAD_LOCK.acquire(timeout=0.25):
        cancel()
    try:
        if path.is_file():
            return path, "cache"
        cancel()
        with requests.get(DOWNLOAD_URL, stream=True, timeout=(8, 30)) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length", 0))
            progress(0, total)
            with atomic_target(path) as temporary:
                received = 0
                with temporary.open("wb") as output:
                    for chunk in response.iter_content(1024 * 1024):
                        cancel()
                        output.write(chunk)
                        received += len(chunk)
                        progress(received, total)
                if not received or (total and received != total):
                    raise ValueError("The GRDI GeoTIFF download is incomplete.")
                with rasterio.open(temporary) as raster:
                    _validate_raster(raster)
        return path, "download"
    finally:
        _DOWNLOAD_LOCK.release()


def _validate_raster(raster) -> None:
    transform = raster.transform
    if (raster.count != 1 or raster.crs != rasterio.crs.CRS.from_epsg(4326)
            or not transform.is_rectilinear or transform.b or transform.d
            or transform.a <= 0 or transform.e >= 0):
        raise ValueError("Expected a north-up, single-band GRDI GeoTIFF in EPSG:4326.")


def _zone_mean(raster, polygon) -> float:
    try:
        window = geometry_window(raster, [polygon])
    except WindowError:
        return np.nan
    grid = raster.read(1, window=window, masked=True).astype(float).filled(np.nan)
    rows, cols = np.where(np.isfinite(grid) & (grid >= 0) & (grid <= 100))
    transform = raster.window_transform(window)
    west, north = transform * (cols, rows)
    east, south = transform * (cols + 1, rows + 1)
    overlap = shapely.intersection(shapely.box(west, south, east, north), polygon)
    weights = shapely.area(overlap) * np.cos(np.deg2rad((north + south) / 2))
    return float(np.average(grid[rows, cols], weights=weights)) if weights.sum() > 0 else np.nan


def sample_grdi(
    areas: gpd.GeoDataFrame, cancel: Callable[[], None], progress: Callable[[int, int], None],
) -> tuple[np.ndarray, Provenance]:
    path, access = _raster_path(cancel, progress)
    zones = areas.to_crs(4326).geometry
    scores = np.full(len(zones), np.nan)
    with rasterio.open(path) as raster:
        _validate_raster(raster)
        for index, polygon in enumerate(zones):
            cancel()
            scores[index] = _zone_mean(raster, polygon)
    link = DOWNLOAD_URL if access != "local" else "local GeoTIFF (user configured)"
    provenance = Provenance(
        source=SOURCE, tier="live" if access == "download" else "cache",
        record_count=int(np.isfinite(scores).sum()), crs="EPSG:4326",
        notes=f"{METHOD} Native resolution: 30 arc-seconds. {SOURCE_URL}; raster: {link}",
    )
    return scores, provenance
