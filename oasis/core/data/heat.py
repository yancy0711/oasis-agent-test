"""Tract heat exposure from Landsat 8/9 Collection 2 Level-2 surface temperature."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import logging
from pathlib import Path
import time
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import geopandas as gpd
import numpy as np
import pandas as pd

from oasis.contracts.domain import Provenance
from oasis.core.cache import atomic_target, atomic_write_text, cache_dir
from oasis.core.spatial.alignment import ensure_crs, pick_metric_crs

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "landsat-c2-l2"
ST_SCALE = 0.00341802
ST_OFFSET_K = 149.0
SR_SCALE = 0.0000275
SR_OFFSET = -0.2
OPTICAL_BANDS = ("blue", "red", "nir08", "swir16", "swir22")
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


def _item_is_readable(item) -> bool:
    """Validate one scene's required TIFF headers."""
    import planetary_computer
    import requests

    for band in ("lwir11", "qa_pixel"):
        for attempt in range(3):
            try:
                response = requests.get(
                    planetary_computer.sign(item.assets[band].href),
                    headers={"Range": "bytes=0-3"},
                    timeout=30,
                )
                response.raise_for_status()
                if not response.content.startswith(TIFF_MAGIC):
                    log.warning(
                        "Skipping corrupt Landsat scene %s: %s is not a TIFF",
                        item.id,
                        band,
                    )
                    return False
                break
            except requests.RequestException as exc:
                if attempt == 2:
                    log.warning(
                        "Skipping unreadable Landsat scene %s after retries: %s",
                        item.id,
                        exc,
                    )
                    return False
                time.sleep(0.5 * 2**attempt)
    return True


def _readable_items(
    items: list,
    max_workers: int = 1,
    cancel_check: Callable[[], None] | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> list:
    """Keep readable scenes, probing independent headers in bounded threads."""
    if not items:
        raise ValueError("no Landsat scenes are available for asset validation")
    worker_count = min(8, max(1, int(max_workers)), len(items))
    if worker_count == 1:
        valid = []
        for index, item in enumerate(items):
            if cancel_check:
                cancel_check()
            if progress_callback:
                progress_callback("Landsat heat: validating scene files", index, len(items))
            if _item_is_readable(item):
                valid.append(item)
        if progress_callback:
            progress_callback("Landsat heat: validating scene files", len(items), len(items))
    else:
        readable = [False] * len(items)
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="landsat-probe",
        ) as executor:
            futures = {
                executor.submit(_item_is_readable, item): index
                for index, item in enumerate(items)
            }
            try:
                for completed, future in enumerate(as_completed(futures), start=1):
                    if cancel_check:
                        cancel_check()
                    readable[futures[future]] = future.result()
                    if progress_callback:
                        progress_callback(
                            "Landsat heat: validating scene files", completed, len(items)
                        )
            except Exception:
                for future in futures:
                    future.cancel()
                raise
        valid = [item for item, ok in zip(items, readable) if ok]
    if not valid:
        raise ValueError("no readable Landsat scenes remain after asset validation")
    return valid


def _geometry_digest(tracts: gpd.GeoDataFrame) -> str:
    """Return a stable digest for tract identifiers and geometries."""
    frame = ensure_crs(tracts[["GEOID", "geometry"]].copy()).sort_values("GEOID")
    digest = hashlib.sha256()
    for row in frame.itertuples():
        digest.update(str(row.GEOID).encode())
        digest.update(row.geometry.wkb)
    return digest.hexdigest()[:16]


def _request_key(tracts: gpd.GeoDataFrame, **params) -> str:
    """Return the cache key for one heat-data request."""
    payload = {**params, "geometry": _geometry_digest(tracts)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]


def _search_items(
    bbox: list[float],
    start_date: str,
    end_date: str,
    cloud_cover: float,
) -> tuple[list, str]:
    """Search or load cached Landsat STAC items."""
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
    atomic_write_text(path, json.dumps(pystac.ItemCollection(items).to_dict()))
    return items, "live"


def _temperature_celsius(dn):
    """Convert Landsat surface-temperature values to Celsius."""
    return dn * ST_SCALE + ST_OFFSET_K - 273.15


def _clear_mask(qa):
    """Return pixels that pass the Landsat quality mask."""
    return (qa.astype("uint16") & QA_BAD_BITS) == 0


def _surface_reflectance(dn):
    """Apply the USGS Collection 2 Level-2 surface-reflectance scaling."""
    return dn * SR_SCALE + SR_OFFSET


def _median_composite(
    items: list,
    bbox: list[float],
    metric_crs: str,
    resolution: float,
    dask_workers: int = 1,
):
    """Build a clear-pixel median surface-temperature composite."""
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
    composite = lst.median("time", skipna=True).compute(
        scheduler="threads", num_workers=min(8, max(1, int(dask_workers)))
    )
    if not np.isfinite(composite.values).any():
        raise ValueError("Landsat composite contains no clear surface-temperature pixels")
    return composite


def _environment_composite(
    items: list,
    bbox: list[float],
    metric_crs: str,
    resolution: float,
    dask_workers: int = 1,
):
    """Build median NDVI, NDBI, and broadband-albedo proxy composites."""
    import planetary_computer
    import xarray as xr
    from odc.stac import stac_load

    usable = [item for item in items if set(OPTICAL_BANDS) | {"qa_pixel"} <= set(item.assets)]
    if not usable:
        raise ValueError("no Landsat scenes contain the optical bands required for NDVI/NDBI/albedo")
    data = stac_load(
        usable,
        bands=[*OPTICAL_BANDS, "qa_pixel"],
        bbox=bbox,
        crs=metric_crs,
        resolution=resolution,
        groupby="solar_day",
        chunks={"x": 1024, "y": 1024},
        patch_url=planetary_computer.sign,
    )
    clear = _clear_mask(data["qa_pixel"].fillna(1))
    reflectance = {
        band: _surface_reflectance(data[band]).where((data[band] > 0) & clear)
        for band in OPTICAL_BANDS
    }
    red, nir, swir = reflectance["red"], reflectance["nir08"], reflectance["swir16"]
    ndvi_denominator = nir + red
    ndbi_denominator = swir + nir
    ndvi = ((nir - red) / ndvi_denominator).where(abs(ndvi_denominator) > 1e-9)
    ndbi = ((swir - nir) / ndbi_denominator).where(abs(ndbi_denominator) > 1e-9)
    # Liang (2001) narrow-to-broadband approximation; report it as a proxy.
    albedo = (
        0.356 * reflectance["blue"] + 0.130 * reflectance["red"]
        + 0.373 * reflectance["nir08"] + 0.085 * reflectance["swir16"]
        + 0.072 * reflectance["swir22"] - 0.0018
    ).where(clear)
    result = xr.Dataset({"mean_ndvi": ndvi, "mean_ndbi": ndbi, "mean_albedo": albedo})
    result = result.median("time", skipna=True).compute(
        scheduler="threads", num_workers=min(8, max(1, int(dask_workers)))
    )
    if not any(np.isfinite(result[name].values).any() for name in result.data_vars):
        raise ValueError("Landsat optical composite contains no clear pixels")
    return result


def _zonal_environment_indices(
    tracts: gpd.GeoDataFrame,
    composite,
    metric_crs: str,
) -> pd.DataFrame:
    """Aggregate derived optical indices onto tract geometries."""
    from rasterstats import zonal_stats

    result = pd.DataFrame({
        "GEOID": tracts["GEOID"].astype("string").str.zfill(11).to_numpy()
    })
    projected = tracts.to_crs(metric_crs)
    for name in ("mean_ndvi", "mean_ndbi", "mean_albedo"):
        values, transform = _grid_definition(composite[name])
        raster = np.where(np.isfinite(values), values, -9999).astype("float32")
        stats = zonal_stats(
            projected.geometry, raster, affine=transform, nodata=-9999, stats=["mean", "count"]
        )
        result[name] = [row["mean"] for row in stats]
        if name == "mean_ndvi":
            result["environment_valid_pixel_count"] = [int(row["count"] or 0) for row in stats]
    return result


def _zonal_mean(tracts: gpd.GeoDataFrame, composite, metric_crs: str) -> pd.DataFrame:
    """Aggregate raster temperature values to Census tracts."""
    from rasterio.transform import from_origin
    from rasterstats import zonal_stats

    x, y = composite.x.values, composite.y.values
    if x.size < 2 or y.size < 2:
        raise ValueError(
            f"Landsat raster grid is too small for zonal statistics: x={x.size}, y={y.size}"
        )
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


def _grid_definition(composite) -> tuple[np.ndarray, Any]:
    """Return north-up values and a matching raster transform."""
    from rasterio.transform import from_origin

    x = np.asarray(getattr(composite.x, "values", composite.x))
    y = np.asarray(getattr(composite.y, "values", composite.y))
    if x.size < 2 or y.size < 2:
        raise ValueError("Landsat raster grid is too small for exposure statistics")
    values = np.asarray(composite.values, dtype="float32")
    if x[0] > x[-1]:
        x, values = x[::-1], values[:, ::-1]
    if y[0] < y[-1]:
        y, values = y[::-1], values[::-1, :]
    xres, yres = abs(float(x[1] - x[0])), abs(float(y[1] - y[0]))
    return values, from_origin(float(x.min()) - xres / 2, float(y.max()) + yres / 2, xres, yres)


def _read_population_on_grid(
    source: str,
    shape: tuple[int, int],
    transform,
    metric_crs: str,
) -> np.ndarray:
    """Window-read a population GeoTIFF and conserve counts on the LST grid."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.warp import reproject, transform_bounds
    from rasterio.windows import Window, from_bounds

    destination = np.zeros(shape, dtype="float64")
    env = {
        "GDAL_HTTP_MULTIRANGE": "YES",
        "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
        "GDAL_HTTP_TIMEOUT": "60",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    }
    with rasterio.Env(**env), rasterio.open(source) as dataset:
        left, bottom, right, top = rasterio.transform.array_bounds(*shape, transform)
        source_bounds = transform_bounds(metric_crs, dataset.crs, left, bottom, right, top, densify_pts=21)
        requested = from_bounds(*source_bounds, transform=dataset.transform).round_offsets().round_lengths()
        window = requested.intersection(Window(0, 0, dataset.width, dataset.height))
        data = dataset.read(1, window=window, masked=True)
        values = np.asarray(data.filled(0), dtype="float64")
        values[~np.isfinite(values) | (values < 0)] = 0
        reproject(
            source=values,
            destination=destination,
            src_transform=dataset.window_transform(window),
            src_crs=dataset.crs,
            dst_transform=transform,
            dst_crs=metric_crs,
            src_nodata=0,
            dst_nodata=0,
            resampling=Resampling.sum,
        )
    return destination


def _population_weighted_zonal(
    tracts: gpd.GeoDataFrame,
    composite,
    metric_crs: str,
    population_source: str,
    tract_population: np.ndarray,
) -> pd.DataFrame:
    """Calibrate fine-grid population to ACS totals and aggregate resident-weighted LST."""
    from rasterio.features import rasterize

    lst, transform = _grid_definition(composite)
    population = _read_population_on_grid(population_source, lst.shape, transform, metric_crs)
    projected = tracts.to_crs(metric_crs)
    labels = rasterize(
        ((geometry, index + 1) for index, geometry in enumerate(projected.geometry)),
        out_shape=lst.shape,
        transform=transform,
        fill=0,
        dtype="int32",
        all_touched=True,
    )
    totals = np.asarray(tract_population, dtype=float)
    if totals.shape != (len(tracts),) or not np.isfinite(totals).all() or (totals < 0).any():
        raise ValueError("tract_population must be a finite non-negative vector aligned with tracts")
    rows = []
    for index, geoid in enumerate(tracts["GEOID"].astype("string").str.zfill(11)):
        cells = labels == index + 1
        temperatures = lst[cells]
        raw_population = population[cells]
        raw_total = float(raw_population.sum())
        calibrated = raw_population * (totals[index] / raw_total) if raw_total > 0 else raw_population
        valid = np.isfinite(temperatures) & (calibrated > 0)
        valid_population = float(calibrated[valid].sum())
        weighted = (
            float(np.average(temperatures[valid], weights=calibrated[valid]))
            if valid_population > 0 else np.nan
        )
        mean = float(np.nanmean(temperatures)) if np.isfinite(temperatures).any() else np.nan
        share = valid_population / totals[index] if totals[index] > 0 else np.nan
        rows.append({
            "GEOID": geoid,
            "mean_lst": mean,
            "population_weighted_lst": weighted,
            "exposure_gap": weighted - mean if np.isfinite(weighted) and np.isfinite(mean) else np.nan,
            "valid_lst_population_share": min(max(share, 0.0), 1.0) if np.isfinite(share) else np.nan,
            "population_grid_total_before_calibration": raw_total,
        })
    return pd.DataFrame(rows)


def _population_weighted_from_polygons(
    tracts: gpd.GeoDataFrame,
    composite,
    metric_crs: str,
    population_areas: gpd.GeoDataFrame,
    tract_population: np.ndarray,
) -> pd.DataFrame:
    """Disaggregate block-group counts over LST cells, then aggregate to tracts."""
    from rasterio.features import rasterize

    if population_areas.crs is None or not {"GEOID", "population"} <= set(population_areas):
        raise ValueError("population_areas must have a CRS, GEOID, population, and geometry")
    lst, transform = _grid_definition(composite)
    fine = population_areas.to_crs(metric_crs)
    grid_population = np.zeros(lst.shape, dtype="float64")
    assigned = 0
    for row in fine.itertuples():
        count = float(row.population) if pd.notna(row.population) else 0.0
        if count <= 0 or row.geometry is None or row.geometry.is_empty:
            continue
        mask = rasterize(
            [(row.geometry, 1)], out_shape=lst.shape, transform=transform,
            fill=0, dtype="uint8", all_touched=False,
        ).astype(bool)
        if not mask.any():
            mask = rasterize(
                [(row.geometry, 1)], out_shape=lst.shape, transform=transform,
                fill=0, dtype="uint8", all_touched=True,
            ).astype(bool)
        if mask.any():
            grid_population[mask] += count / int(mask.sum())
            assigned += 1
    if assigned == 0 or grid_population.sum() <= 0:
        raise ValueError("No block-group population could be aligned to the Landsat grid")

    projected = tracts.to_crs(metric_crs)
    labels = rasterize(
        ((geometry, index + 1) for index, geometry in enumerate(projected.geometry)),
        out_shape=lst.shape, transform=transform, fill=0, dtype="int32", all_touched=True,
    )
    totals = np.asarray(tract_population, dtype=float)
    rows = []
    for index, geoid in enumerate(tracts["GEOID"].astype("string").str.zfill(11)):
        cells = labels == index + 1
        temperatures, raw_population = lst[cells], grid_population[cells]
        raw_total = float(raw_population.sum())
        calibrated = raw_population * (totals[index] / raw_total) if raw_total > 0 else raw_population
        valid = np.isfinite(temperatures) & (calibrated > 0)
        valid_population = float(calibrated[valid].sum())
        weighted = float(np.average(temperatures[valid], weights=calibrated[valid])) if valid_population > 0 else np.nan
        mean = float(np.nanmean(temperatures)) if np.isfinite(temperatures).any() else np.nan
        share = valid_population / totals[index] if totals[index] > 0 else np.nan
        rows.append({
            "GEOID": geoid, "mean_lst": mean, "population_weighted_lst": weighted,
            "exposure_gap": weighted - mean if np.isfinite(weighted) and np.isfinite(mean) else np.nan,
            "valid_lst_population_share": min(max(share, 0.0), 1.0) if np.isfinite(share) else np.nan,
            "population_grid_total_before_calibration": raw_total,
        })
    return pd.DataFrame(rows)


def fetch_heat_exposure(
    tracts: gpd.GeoDataFrame,
    start_date: str,
    end_date: str,
    cloud_cover: float = 20,
    resolution: float = 120,
    *,
    population_source: dict[str, Any] | None = None,
    tract_population: np.ndarray | None = None,
    population_areas: gpd.GeoDataFrame | None = None,
    readable_workers: int = 1,
    dask_workers: int = 1,
    progress_callback: Callable[[str, int, int], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> tuple[pd.DataFrame, Provenance]:
    """Return tract mean and, when configured, ACS-calibrated population-weighted LST."""
    if cancel_check:
        cancel_check()
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
        "population_source": (population_source or {}).get("url", (population_source or {}).get("provider")),
        "tract_population": (
            hashlib.sha256(np.asarray(tract_population, dtype=float).tobytes()).hexdigest()[:16]
            if tract_population is not None else None
        ),
        "population_areas": (
            hashlib.sha256(
                pd.util.hash_pandas_object(
                    population_areas[["GEOID", "population"]].sort_values("GEOID"),
                    index=False,
                ).values.tobytes()
            ).hexdigest()[:16]
            if population_areas is not None else None
        ),
    }
    path: Path = cache_dir("heat") / f"landsat_lst_{_request_key(study, **params)}.csv"
    if progress_callback:
        progress_callback("Landsat heat: checking tract cache", 0, 4)
    cached_mean = None
    if path.exists():
        result = pd.read_csv(path, dtype={"GEOID": "string"})
        weighted_cache_ok = (
            not population_source
            or (
                "population_weighted_lst" in result
                and result["population_weighted_lst"].notna().any()
            )
        )
        if weighted_cache_ok:
            weighted_note = (
                f"population-weighted with {(population_source or {}).get('provider')} "
                f"{(population_source or {}).get('year', '')} "
                f"{(population_source or {}).get('resolution', '')}; "
                if population_source else ""
            )
            if progress_callback:
                progress_callback("Landsat heat: loaded tract cache", 4, 4)
            return result, Provenance(
                source="Landsat 8/9 Collection 2 Level-2 via Microsoft Planetary Computer",
                tier="cache", record_count=len(result),
                notes=f"{weighted_note}tract cache={path.name}",
            )
        if "mean_lst" in result and result["mean_lst"].notna().any():
            cached_mean = result
        log.info("Ignoring mean-only heat cache because population weighting is requested: %s", path)

    try:
        if progress_callback:
            progress_callback("Landsat heat: searching scenes", 1, 4)
        items, search_tier = _search_items(bbox, start_date, end_date, cloud_cover)
        if cancel_check:
            cancel_check()
        items = _readable_items(
            items,
            max_workers=readable_workers,
            cancel_check=cancel_check,
            progress_callback=progress_callback,
        )
        metric_crs = pick_metric_crs(study)
        if cancel_check:
            cancel_check()
        if progress_callback:
            progress_callback("Landsat heat: reading thermal windows and compositing", 2, 4)
        composite = _median_composite(
            items,
            bbox,
            metric_crs,
            resolution,
            dask_workers=dask_workers,
        )
    except Exception as exc:
        if cached_mean is None:
            raise
        if progress_callback:
            progress_callback("Landsat heat: retained cached tract means", 4, 4)
        return cached_mean, Provenance(
            source="Landsat 8/9 Collection 2 Level-2 via Microsoft Planetary Computer",
            tier="cache", record_count=len(cached_mean),
            notes=(
                f"cached tract-mean LST retained after population-weighting refresh failed "
                f"({type(exc).__name__}: {exc}); cache={path.name}"
            ),
        )
    if cancel_check:
        cancel_check()
    if progress_callback:
        progress_callback("Landsat heat: population weighting and tract aggregation", 3, 4)
    weighting_note = "population weighting not requested"
    weighting_failed = False
    if population_source and tract_population is not None:
        try:
            if population_source.get("provider") == "census_block_group":
                if population_areas is None:
                    raise ValueError("Census block-group population areas were not supplied")
                result = _population_weighted_from_polygons(
                    study, composite, metric_crs, population_areas,
                    np.asarray(tract_population, dtype=float),
                )
            else:
                result = _population_weighted_zonal(
                    study, composite, metric_crs, str(population_source["url"]),
                    np.asarray(tract_population, dtype=float),
                )
            weighting_note = (
                f"population-weighted with {population_source.get('provider', 'GeoTIFF')} "
                f"{population_source.get('year', '')} {population_source.get('resolution', '')}; "
                "sub-tract counts calibrated to ACS tract totals"
            )
        except Exception as exc:
            weighting_failed = True
            log.warning("Fine-grid population weighting failed; retaining tract mean LST: %s", exc)
            result = _zonal_mean(study, composite, metric_crs)
            result["population_weighted_lst"] = np.nan
            result["exposure_gap"] = np.nan
            result["valid_lst_population_share"] = np.nan
            weighting_note = f"population weighting unavailable ({type(exc).__name__}: {exc})"
    else:
        result = _zonal_mean(study, composite, metric_crs)
    if cancel_check:
        cancel_check()
    # Persist tract means even when population weighting fails. Later runs retry
    # weighting but retain a valid heat layer if a remote refresh is unavailable.
    with atomic_target(path) as temporary:
        result.to_csv(temporary, index=False)
    if progress_callback:
        progress_callback("Landsat heat: tract exposure ready", 4, 4)
    return result, Provenance(
        source="Landsat 8/9 Collection 2 Level-2 via Microsoft Planetary Computer",
        tier="live", record_count=len(result), crs=metric_crs,
        notes=(
            f"{len(items)} L2SP scenes; {start_date}/{end_date}; cloud<{cloud_cover}%; "
            f"median composite at {resolution} m; {weighting_note}; "
            f"STAC search={search_tier}; cache={path.name}"
        ),
    )
