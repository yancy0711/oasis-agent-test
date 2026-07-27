"""Tract heat exposure from Landsat 8/9 Collection 2 Level-2 surface temperature."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import geopandas as gpd
import numpy as np
import pandas as pd

from agent.schemas import Provenance
from robustness.cache import cache_dir
from tools.spatial.alignment import ensure_crs, pick_metric_crs

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "landsat-c2-l2"
ST_SCALE = 0.00341802
ST_OFFSET_K = 149.0
QA_BAD_BITS = sum(1 << bit for bit in (0, 1, 2, 3, 4, 5, 7))
TIFF_MAGIC = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
log = logging.getLogger(__name__)


def _strip_asset_signatures(items: list) -> list:
    """Remove expiring Azure SAS queries so each raster load can sign afresh."""
    for item in items:
        for asset in item.assets.values():
            parts = urlsplit(asset.href)
            if parts.netloc.endswith(".blob.core.windows.net") and parts.query:
                asset.href = urlunsplit((parts.scheme, parts.netloc, parts.path, "", parts.fragment))
    return items


def _readable_items(items: list) -> list:
    """Keep scenes whose required assets expose a valid TIFF header."""
    import planetary_computer
    import requests

    valid = []
    for item in items:
        for band in ("lwir11", "qa_pixel"):
            response = requests.get(planetary_computer.sign(item.assets[band].href),
                                    headers={"Range": "bytes=0-3"}, timeout=30)
            response.raise_for_status()
            if not response.content.startswith(TIFF_MAGIC):
                log.warning("Skipping corrupt Landsat scene %s: %s is not a TIFF", item.id, band)
                break
        else: valid.append(item)
    if not valid: raise ValueError("no readable Landsat scenes remain after asset validation")
    return valid


def _geometry_digest(tracts: gpd.GeoDataFrame) -> str:
    frame = ensure_crs(tracts[["GEOID", "geometry"]].copy()).sort_values("GEOID")
    digest = hashlib.sha256()
    for row in frame.itertuples():
        digest.update(str(row.GEOID).encode())
        digest.update(row.geometry.wkb)
    return digest.hexdigest()[:16]


def _request_key(tracts: gpd.GeoDataFrame, **params) -> str:
    payload = {**params, "geometry": _geometry_digest(tracts)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]


def _search_items(
    bbox: list[float],
    start_date: str,
    end_date: str,
    cloud_cover: float,
) -> tuple[list, str]:
    import pystac
    import pystac_client

    query = {
        "collection": COLLECTION,
        "bbox": bbox,
        "datetime": f"{start_date}/{end_date}",
        "cloud_cover_lt": cloud_cover,
        "platforms": ["landsat-8", "landsat-9"],
    }
    path = cache_dir("heat") / f"stac_{hashlib.sha256(json.dumps(query, sort_keys=True).encode()).hexdigest()[:20]}.json"
    if path.exists():
        items = list(pystac.ItemCollection.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        return _strip_asset_signatures(items), "cache"

    catalog = pystac_client.Client.open(STAC_URL)
    items = list(catalog.search(
        collections=[COLLECTION], bbox=bbox, datetime=query["datetime"],
        query={
            "eo:cloud_cover": {"lt": cloud_cover},
            "platform": {"in": query["platforms"]},
        },
    ).items())
    items = [
        item for item in items
        if item.properties.get("landsat:correction") == "L2SP"
        and {"lwir11", "qa_pixel"} <= set(item.assets)
    ]
    if not items:
        raise ValueError("no Landsat 8/9 L2SP surface-temperature scenes matched the query")
    items = _strip_asset_signatures(items)
    path.write_text(json.dumps(pystac.ItemCollection(items).to_dict()), encoding="utf-8")
    return items, "live"


def _temperature_celsius(dn):
    return dn * ST_SCALE + ST_OFFSET_K - 273.15


def _clear_mask(qa):
    return (qa.astype("uint16") & QA_BAD_BITS) == 0


def _median_composite(
    items: list,
    bbox: list[float],
    metric_crs: str,
    resolution: float,
):
    import planetary_computer
    from odc.stac import stac_load

    data = stac_load(
        items,
        bands=["lwir11", "qa_pixel"],
        bbox=bbox,
        crs=metric_crs,
        resolution=resolution,
        groupby="solar_day",
        chunks={"x": 1024, "y": 1024},
        patch_url=planetary_computer.sign,
    )
    dn = data["lwir11"]
    qa = data["qa_pixel"].fillna(1)
    lst = _temperature_celsius(dn).where((dn >= 293) & _clear_mask(qa))
    composite = lst.median("time", skipna=True).compute()
    if not np.isfinite(composite.values).any():
        raise ValueError("Landsat composite contains no clear surface-temperature pixels")
    return composite


def _zonal_mean(tracts: gpd.GeoDataFrame, composite, metric_crs: str) -> pd.DataFrame:
    from rasterio.transform import from_origin
    from rasterstats import zonal_stats

    x, y = composite.x.values, composite.y.values
    xres, yres = abs(float(x[1] - x[0])), abs(float(y[1] - y[0]))
    transform = from_origin(float(x.min()) - xres / 2, float(y.max()) + yres / 2, xres, yres)
    values = np.where(np.isfinite(composite.values), composite.values, -9999).astype("float32")
    stats = zonal_stats(
        tracts.to_crs(metric_crs).geometry,
        values,
        affine=transform,
        nodata=-9999,
        stats=["mean"],
    )
    return pd.DataFrame({
        "GEOID": tracts["GEOID"].astype("string").str.zfill(11).to_numpy(),
        "mean_lst": [row["mean"] for row in stats],
    })


def fetch_heat_exposure(
    tracts: gpd.GeoDataFrame,
    start_date: str,
    end_date: str,
    cloud_cover: float = 20,
    resolution: float = 120,
) -> tuple[pd.DataFrame, Provenance]:
    """Return `GEOID, mean_lst`; an identical request reuses the tract CSV."""
    if tracts.crs is None or "GEOID" not in tracts or tracts["GEOID"].duplicated().any():
        raise ValueError("tracts must have a CRS and unique GEOID values")
    if resolution <= 0 or not 0 < cloud_cover <= 100:
        raise ValueError("resolution must be positive and cloud_cover must be in (0, 100]")

    study = ensure_crs(tracts.copy())
    bbox = study.total_bounds.astype(float).tolist()
    params = {
        "start_date": start_date,
        "end_date": end_date,
        "cloud_cover": cloud_cover,
        "resolution": resolution,
        "bbox": bbox,
    }
    path: Path = cache_dir("heat") / f"landsat_lst_{_request_key(study, **params)}.csv"
    if path.exists():
        result = pd.read_csv(path, dtype={"GEOID": "string"})
        return result, Provenance(
            source="Landsat 8/9 Collection 2 Level-2 via Microsoft Planetary Computer",
            tier="cache", record_count=len(result), notes=f"tract cache={path.name}",
        )

    items, search_tier = _search_items(bbox, start_date, end_date, cloud_cover)
    items = _readable_items(items)
    metric_crs = pick_metric_crs(study)
    composite = _median_composite(items, bbox, metric_crs, resolution)
    result = _zonal_mean(study, composite, metric_crs)
    result.to_csv(path, index=False)
    return result, Provenance(
        source="Landsat 8/9 Collection 2 Level-2 via Microsoft Planetary Computer",
        tier="live", record_count=len(result), crs=metric_crs,
        notes=(
            f"{len(items)} L2SP scenes; {start_date}/{end_date}; cloud<{cloud_cover}%; "
            f"median composite at {resolution} m; STAC search={search_tier}; cache={path.name}"
        ),
    )
