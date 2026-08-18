"""Lightweight tract built-environment indices from Landsat surface reflectance."""
from __future__ import annotations

from collections.abc import Callable
import hashlib
import json

import geopandas as gpd
import pandas as pd

from oasis.contracts.domain import Provenance
from oasis.core.cache import atomic_target, cache_dir
from oasis.core.data.heat import (
    _environment_composite,
    _geometry_digest,
    _search_items,
    _zonal_environment_indices,
)
from oasis.core.spatial.alignment import ensure_crs, pick_metric_crs


def derive_environment_burden(indices: pd.DataFrame) -> pd.DataFrame:
    """Return a transparent [0,1] passive heat-burden score from tract indices.

    Higher NDBI increases burden; higher NDVI and albedo reduce it. Each component
    is percentile-normalized, then available components are averaged. This is a
    prioritization indicator, not a causal or physiological heat-risk estimate.
    """
    required = {"mean_ndvi", "mean_ndbi", "mean_albedo"}
    if not required <= set(indices):
        raise ValueError(f"environment indices are missing {sorted(required - set(indices))}")

    def percentile(series: pd.Series) -> pd.Series:
        numeric = pd.to_numeric(series, errors="coerce")
        valid = numeric.notna()
        result = pd.Series(float("nan"), index=series.index, dtype=float)
        if not valid.any():
            return result
        if numeric[valid].nunique() <= 1:
            result.loc[valid] = 0.5
        else:
            result.loc[valid] = (numeric[valid].rank(method="average") - 1) / (valid.sum() - 1)
        return result

    components = pd.DataFrame({
        "built_up_burden": percentile(indices["mean_ndbi"]),
        "low_greenness_burden": 1 - percentile(indices["mean_ndvi"]),
        "low_albedo_burden": 1 - percentile(indices["mean_albedo"]),
    }, index=indices.index)
    score = components.mean(axis=1, skipna=True)
    fill = float(score.median()) if score.notna().any() else 0.5
    result = components.copy()
    result["environment_components_available"] = components.notna().sum(axis=1)
    result["environment_burden"] = score.fillna(fill).clip(0, 1)
    return result


def fetch_environment_indices(
    tracts: gpd.GeoDataFrame,
    start_date: str,
    end_date: str,
    cloud_cover: float = 20,
    resolution: float = 120,
    *,
    dask_workers: int = 1,
    cancel_check: Callable[[], None] | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> tuple[pd.DataFrame, Provenance]:
    """Return cached tract NDVI, NDBI, and broadband-albedo proxy means."""
    if tracts.crs is None or "GEOID" not in tracts or tracts["GEOID"].duplicated().any():
        raise ValueError("tracts must have a CRS and unique GEOID values")
    study = ensure_crs(tracts.copy())
    bbox = study.total_bounds.astype(float).tolist()
    params = {
        "geometry": _geometry_digest(study), "start_date": start_date,
        "end_date": end_date, "cloud_cover": cloud_cover,
        "resolution": resolution, "version": "landsat_environment_v1",
    }
    key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:20]
    path = cache_dir("environment") / f"landsat_indices_{key}.csv"
    if progress_callback:
        progress_callback("Built environment: checking tract cache", 0, 4)
    if path.exists():
        result = pd.read_csv(path, dtype={"GEOID": "string"})
        if {"mean_ndvi", "mean_ndbi", "mean_albedo"} <= set(result):
            if progress_callback:
                progress_callback("Built environment: loaded tract cache", 4, 4)
            return result, Provenance(
                source="Landsat 8/9 Collection 2 Level-2 surface reflectance",
                tier="cache", record_count=len(result),
                notes=f"NDVI, NDBI, broadband albedo proxy; tract cache={path.name}",
            )
    if cancel_check:
        cancel_check()
    if progress_callback:
        progress_callback("Built environment: searching Landsat scenes", 1, 4)
    items, search_tier = _search_items(bbox, start_date, end_date, cloud_cover)
    metric_crs = pick_metric_crs(study)
    if cancel_check:
        cancel_check()
    if progress_callback:
        progress_callback("Built environment: reading optical windows and compositing", 2, 4)
    composite = _environment_composite(
        items, bbox, metric_crs, resolution, dask_workers=dask_workers
    )
    if cancel_check:
        cancel_check()
    if progress_callback:
        progress_callback("Built environment: aggregating indices to tracts", 3, 4)
    result = _zonal_environment_indices(study, composite, metric_crs)
    with atomic_target(path) as temporary:
        result.to_csv(temporary, index=False)
    if progress_callback:
        progress_callback("Built environment: tract indicators ready", 4, 4)
    return result, Provenance(
        source="Landsat 8/9 Collection 2 Level-2 surface reflectance",
        tier="live", record_count=len(result), crs=metric_crs,
        notes=(
            f"{len(items)} scenes; {start_date}/{end_date}; cloud<{cloud_cover}%; "
            f"clear-pixel median at {resolution} m; NDVI=(NIR-red)/(NIR+red); "
            f"NDBI=(SWIR1-NIR)/(SWIR1+NIR); broadband albedo proxy; "
            f"STAC search={search_tier}; cache={path.name}"
        ),
    )
