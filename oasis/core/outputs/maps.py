"""MapLibre-ready GeoJSON outputs with explicit CRS and compact properties."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd

from oasis.contracts.domain import SiteSolution, TaskSpec
from oasis.core.spatial.alignment import ensure_crs


_MODE_LABELS = {
    "walking": "Walking",
    "cycling": "Cycling",
    "driving": "Driving",
    "walk_drive_equity": "Car-access equity: households without a car walk; others drive",
}


def _candidate_name(row: pd.Series) -> str | None:
    """Return the first meaningful OSM name without falling back to an ID."""
    for field in ("name", "official_name", "short_name", "brand", "operator"):
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _json_value(value: Any) -> Any:
    """Convert pandas/numpy scalars and missing values into strict JSON values."""
    if value is None or bool(pd.isna(value)):
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def _tract_properties(row: pd.Series, indicator: str) -> dict[str, Any]:
    """Expose stable, presentation-ready tract properties to the web map."""
    properties: dict[str, Any] = {
        "layer_kind": "tract",
        "GEOID": str(row.get("GEOID", "")),
    }
    specs = (
        ("total_population", "population", 0, 1),
        ("elderly_ratio", "elderly_pct", 1, 100),
        ("poverty_rate", "poverty_pct", 1, 100),
        ("heat_exposure", "heat_c", 1, 1),
        ("population_weighted_lst", "population_weighted_heat_c", 1, 1),
        ("mean_lst", "tract_mean_heat_c", 1, 1),
        ("exposure_gap", "heat_exposure_gap_c", 1, 1),
        ("valid_lst_population_share", "valid_heat_population_pct", 1, 100),
        ("mean_ndvi", "mean_ndvi", 3, 1),
        ("mean_ndbi", "mean_ndbi", 3, 1),
        ("mean_albedo", "mean_albedo", 3, 1),
        ("nearest_candidate_minutes", "nearest_candidate_minutes", 1, 1),
        ("reachable_candidates", "reachable_candidates", 0, 1),
        ("reachability_rank", "reachability_rank", 0, 1),
    )
    for source, target, digits, scale in specs:
        value = pd.to_numeric(row.get(source), errors="coerce")
        properties[target] = None if pd.isna(value) else round(float(value) * scale, digits)
    value = pd.to_numeric(row.get(indicator), errors="coerce")
    properties["indicator_value"] = None if pd.isna(value) else float(value)
    method = row.get("heat_exposure_method")
    method_labels = {
        "population_weighted_grid": "Population weighted using the 100 m population grid",
        "population_weighted_block_group": "Population weighted using Census block groups",
        "tract_mean_fallback": "Tract mean used because population weighting was unavailable or incomplete",
        "unavailable_neutral": "Landsat was unavailable; heat was neutralized and is not interpretable as temperature",
    }
    properties["heat_exposure_method"] = method_labels.get(method) if isinstance(method, str) else None
    properties["is_hardest_reachability"] = bool(row.get("is_hardest_reachability", False))
    if properties.get("valid_heat_population_pct") is not None and properties["valid_heat_population_pct"] >= 100:
        properties["valid_heat_population_pct"] = None
    if method == "tract_mean_fallback":
        for key in (
            "population_weighted_heat_c", "heat_exposure_gap_c", "valid_heat_population_pct"
        ):
            properties[key] = None
    if method == "unavailable_neutral":
        for key in ("heat_c", "population_weighted_heat_c", "tract_mean_heat_c",
                    "heat_exposure_gap_c", "valid_heat_population_pct", "indicator_value"):
            properties[key] = None
    return properties


def _site_properties(row: pd.Series, task: TaskSpec | None) -> dict[str, Any]:
    """Expose only available selected-candidate evidence as presentation fields."""
    status = str(row.get("hours_status", "not_evaluated"))
    expression = row.get("opening_hours")
    declared = expression.strip() if isinstance(expression, str) and expression.strip() else None
    website = next(
        (
            row.get(field).strip()
            for field in ("website", "contact:website")
            if isinstance(row.get(field), str) and row.get(field).strip()
        ),
        None,
    )
    window = getattr(task, "operating_window", None)
    facility_type = next((row.get(field) for field in ("amenity", "leisure")
                          if isinstance(row.get(field), str) and row.get(field).strip()), None)
    properties = {
        "layer_kind": "selected_site",
        "candidate_id": str(row.get("cand_id", "unknown")),
        "name": _candidate_name(row) or "Unnamed candidate facility",
        "facility_type": facility_type.replace("_", " ").title() if facility_type else None,
        "mobility": _MODE_LABELS.get(
            getattr(task, "mobility_scenario", ""),
            getattr(task, "mobility_scenario", "Not specified"),
        ),
    }
    access_label = {
            "osm_public": "OSM marks access as public/permissive",
            "conditional": "OSM access is conditional; verify the stated restriction",
        }.get(str(row.get("public_access_status", "unknown")))
    if access_label: properties["public_access"] = access_label
    if str(row.get("cooling_service_status", "unverified")) == "verified":
        properties["cooling_service"] = "Verified public cooling service"
    if declared: properties["opening_hours"] = declared
    hours_label = {
            "open": "Open for the full requested window",
            "partial": "Open for only part of the requested window",
            "closed": "Closed in the requested window",
        }.get(status)
    if hours_label: properties["hours_label"] = hours_label
    if window and hours_label: properties["requested_window"] = (
            f"{window.date.isoformat()} {window.start.isoformat(timespec='minutes')}-"
            f"{window.end.isoformat(timespec='minutes')}"
        )
    if str(row.get("fee_status", "unknown")) == "free": properties["fee"] = "No fee tagged in OpenStreetMap"
    if website: properties["website"] = website
    observed = _json_value(row.get("capacity_observed_people"))
    footprint = _json_value(row.get("footprint_area_m2"))
    if observed is not None: properties["observed_capacity_people"] = observed
    if footprint is not None: properties["footprint_area_m2"] = footprint
    evidence = {
            "observed_osm_capacity": "Observed people capacity from OpenStreetMap",
            "relative_floor_area_proxy": "Relative floor-area evidence only; not a people capacity",
        }.get(str(row.get("capacity_evidence", "unknown")))
    if evidence: properties["capacity_evidence"] = evidence
    return properties


def _feature(geometry: Any, properties: dict[str, Any]) -> dict[str, Any]:
    """Build one JSON-safe GeoJSON feature from a Shapely geometry."""
    return {
        "type": "Feature",
        "geometry": geometry.__geo_interface__,
        "properties": {key: _json_value(value) for key, value in properties.items()},
    }


def _write_map(
    tracts: gpd.GeoDataFrame,
    out_path: str,
    *,
    indicator: str,
    candidates: gpd.GeoDataFrame | None = None,
    solution: SiteSolution | None = None,
    boundary: gpd.GeoDataFrame | None = None,
    task: TaskSpec | None = None,
    comparison_selections: dict[str, tuple[str, list[str]]] | None = None,
) -> str:
    """Write a self-describing FeatureCollection consumed directly by MapLibre."""
    tracts = ensure_crs(tracts)
    features = [
        _feature(row.geometry, _tract_properties(row, indicator))
        for _, row in tracts.iterrows()
        if row.geometry is not None and not row.geometry.is_empty
    ]
    if boundary is not None and not boundary.empty:
        for _, row in ensure_crs(boundary).iterrows():
            if row.geometry is not None and not row.geometry.is_empty:
                features.append(_feature(row.geometry, {"layer_kind": "boundary"}))
    if candidates is not None and solution is not None and not candidates.empty:
        candidates = ensure_crs(candidates)
        selected_ids = set(map(str, solution.selected_ids))
        selected = candidates[candidates["cand_id"].astype(str).isin(selected_ids)]
        for _, row in selected.iterrows():
            if row.geometry is not None and not row.geometry.is_empty:
                features.append(_feature(row.geometry, _site_properties(row, task)))
    if candidates is not None and comparison_selections and not candidates.empty:
        candidates = ensure_crs(candidates)
        candidate_ids = candidates["cand_id"].astype(str)
        for role, (label, selected_ids) in comparison_selections.items():
            selected = candidates[candidate_ids.isin(set(map(str, selected_ids)))]
            for _, row in selected.iterrows():
                if row.geometry is None or row.geometry.is_empty:
                    continue
                properties = _site_properties(row, task)
                properties.update({
                    "layer_kind": "comparison_site",
                    "comparison_role": role,
                    "comparison_label": label,
                })
                features.append(_feature(row.geometry, properties))

    minx, miny, maxx, maxy = map(float, tracts.total_bounds)
    values = pd.to_numeric(tracts.get(indicator), errors="coerce")
    finite = values.dropna() if isinstance(values, pd.Series) else pd.Series(dtype=float)
    payload = {
        "type": "FeatureCollection",
        "metadata": {
            "renderer": "maplibre",
            "indicator": indicator,
            "indicator_label": (
                "Heat exposure (°C)" if indicator == "heat_exposure" else indicator.replace("_", " ").title()
            ),
            "value_range": [float(finite.min()), float(finite.max())] if not finite.empty else None,
            "bounds": [[minx, miny], [maxx, maxy]],
        },
        "features": features,
    }
    Path(out_path).write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    return out_path


def coverage_map(
    tracts: gpd.GeoDataFrame,
    candidates: gpd.GeoDataFrame,
    solution: SiteSolution,
    indicator: str,
    out_path: str,
    boundary: gpd.GeoDataFrame | None = None,
    *,
    task: TaskSpec | None = None,
) -> str:
    """Write tract, selected-site, and study-boundary data for the MapLibre client."""
    return _write_map(
        tracts,
        out_path,
        indicator=indicator,
        candidates=candidates,
        solution=solution,
        boundary=boundary,
        task=task,
    )


def comparison_map(
    tracts: gpd.GeoDataFrame,
    candidates: gpd.GeoDataFrame,
    selections: dict[str, tuple[str, list[str]]],
    out_path: str,
    boundary: gpd.GeoDataFrame | None = None,
    *,
    task: TaskSpec | None = None,
) -> str:
    """Write one tract map with distinct baseline and alternative site markers."""
    return _write_map(
        tracts,
        out_path,
        indicator="heat_exposure",
        candidates=candidates,
        boundary=boundary,
        task=task,
        comparison_selections=selections,
    )


def heat_exposure_map(
    tracts: gpd.GeoDataFrame,
    out_path: str,
    boundary: gpd.GeoDataFrame | None = None,
) -> str:
    """Write a standalone tract heat-exposure dataset for the MapLibre client."""
    return _write_map(tracts, out_path, indicator="heat_exposure", boundary=boundary)


def reachability_map(
    tracts: gpd.GeoDataFrame,
    rows: list[dict[str, Any]],
    out_path: str,
    boundary: gpd.GeoDataFrame | None = None,
) -> str:
    """Write tract travel-time diagnostics for the hardest-to-reach review."""
    values = pd.DataFrame(rows)
    if values.empty:
        raise ValueError("Reachability diagnostics contain no tract rows")
    values["GEOID"] = values["demand_id"].astype(str).str.split(":").str[0]
    values = values.groupby("GEOID", as_index=False).agg(
        nearest_candidate_minutes=("nearest_minutes", "max"),
        reachable_candidates=("reachable_candidates", "min"),
        reachability_rank=("rank", "min"),
    )
    values["is_hardest_reachability"] = values["reachability_rank"] == 1
    frame = tracts.copy()
    frame["GEOID"] = frame["GEOID"].astype(str)
    frame = frame.merge(values, on="GEOID", how="left", validate="one_to_one")
    return _write_map(
        frame, out_path, indicator="nearest_candidate_minutes", boundary=boundary
    )
