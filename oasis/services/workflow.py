"""Coarse baseline and diagnostic tools shared by the pipeline and agent."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import wraps
from pathlib import Path
from typing import Callable

import geopandas as gpd
import numpy as np
import pandas as pd

from oasis.agent.context import Context
from oasis.contracts.domain import Provenance, TaskSpec, ToolError, ToolResult, ValueWeights
from oasis.core.outputs.charts import heat_analysis_chart, pareto_chart
from oasis.core.outputs.maps import coverage_map, heat_exposure_map
from oasis.core.outputs.report import render_heat_report, render_report
from oasis.core.cache import scoped_cache
from oasis.core.cancellation import RunCancelled
from oasis.core.data.census import derive_indicators, fetch_acs, fetch_county_geometry, fetch_tract_geometry
from oasis.core.data.geocoding import AmbiguousPlaceError, resolve_fips, resolve_place
from oasis.core.data.heat import fetch_heat_exposure
from oasis.core.data.osm import fetch_candidates
from oasis.core.data.opening_hours import filter_candidates_by_hours
from oasis.core.optimization.mobility import compose_demand, scenario_modes
from oasis.core.optimization.models import (build_demand_weights, minimum_coverage_threshold,
                                       solve_lscp, solve_mclp)
from oasis.core.optimization.pareto import coverage_pareto_sweep, non_dominated
from oasis.services.registry import registry
from oasis.core.spatial.accessibility import (
    haversine_matrix, sparse_network_matrix, validate_travel_matrix,
)
from oasis.core.spatial.alignment import centroids, pick_metric_crs

TOOL_CHAIN = ["resolve_study_area", "prepare_demand", "fetch_candidate_sites",
              "build_travel_matrix", "optimize_sites", "generate_outputs"]
log = logging.getLogger(__name__)


def _guard(name: str):
    """Convert uncaught workflow exceptions into structured tool failures."""
    def decorate(fn: Callable):
        """Wrap one registered workflow callable."""
        @wraps(fn)
        def wrapped(*args, **kwargs):
            """Execute a tool and preserve the ToolResult contract on failure."""
            try: return fn(*args, **kwargs)
            except RunCancelled: raise
            except Exception as exc:
                return ToolResult(ok=False, tool=name, error=ToolError(
                    kind="ambiguous" if isinstance(exc, AmbiguousPlaceError) else (
                         "not_found" if isinstance(exc, (KeyError, ValueError)) else "unknown"),
                    message=str(exc), retryable=False))
        return wrapped
    return decorate


def _cfg(ctx: Context, section: str) -> dict:
    """Return one configuration section from the session context."""
    return ctx.get("config", {}).get(section, {})


def _decision_inputs(demand: dict, travel: dict) -> tuple[np.ndarray, ...]:
    """Return aligned arrays for either tract or grouped multimodal demand."""
    return (
        np.asarray(travel["matrix"], dtype=float),
        np.asarray(travel.get("population", demand["population"]), dtype=float),
        np.asarray(travel.get("vulnerable_mask", demand["vulnerable_mask"]), dtype=bool),
        travel.get("indicators", demand.get("indicators", {})),
    )


def _coverage_mask(demand: dict, group: str, travel: dict | None = None) -> np.ndarray:
    """Return the deterministic demand subset named by a public tool argument."""
    population = travel.get("population", demand["population"]) if travel else demand["population"]
    vulnerable = (
        travel.get("vulnerable_mask", demand["vulnerable_mask"])
        if travel else demand["vulnerable_mask"]
    )
    if group == "all": return np.ones(len(population), dtype=bool)
    if group == "vulnerable": return np.asarray(vulnerable, dtype=bool)
    raise ValueError(f"Unsupported demand group: {group}")


def _filter_candidate_hours(ctx: Context, candidates: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, dict]:
    """Apply one confirmed operating window and retain its quality summary."""
    task = ctx.get("task", None)
    filtered, summary = filter_candidates_by_hours(
        candidates, getattr(task, "operating_window", None)
    )
    if filtered.empty:
        raise ValueError("No candidate sites remain under the confirmed operating-hours policy")
    quality = ctx.get("quality", {})
    quality["operating_hours"] = summary
    ctx.put("quality", quality)
    return filtered, summary


def _candidate_label(row: pd.Series) -> str:
    """Return a stable human-readable label for one candidate result."""
    for field in ("name", "official_name", "short_name", "brand", "operator"):
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"Candidate {row['cand_id']}"


def _filter_candidate_access(
    candidates: gpd.GeoDataFrame,
) -> tuple[gpd.GeoDataFrame, dict]:
    """Exclude explicitly restricted facilities while preserving unknown access as evidence."""
    access = (
        candidates["access"].fillna("").astype(str).str.strip().str.lower()
        if "access" in candidates
        else pd.Series("", index=candidates.index, dtype=str)
    )
    restricted_values = {"no", "private", "customers", "members", "permit"}
    restricted = access.isin(restricted_values)
    result = candidates.loc[~restricted].copy().reset_index(drop=True)
    retained_access = access.loc[~restricted].reset_index(drop=True)
    result["public_access_status"] = np.select(
        [retained_access.isin({"yes", "permissive"}), retained_access.eq("")],
        ["osm_public", "unknown"],
        default="conditional",
    )
    if "cooling_service_status" not in result:
        result["cooling_service_status"] = "unverified"
    summary = {
        "potential_candidates": len(candidates),
        "retained_candidates": len(result),
        "restricted_candidates": int(restricted.sum()),
        "unknown_access_candidates": int(retained_access.eq("").sum()),
    }
    return result, summary


def _land_area_km2(areas: gpd.GeoDataFrame) -> pd.Series:
    """Return TIGER land area, falling back to projected polygon area."""
    geometry_area = areas.to_crs(pick_metric_crs(areas)).area / 1_000_000
    if "ALAND" not in areas: return geometry_area
    land_area = pd.to_numeric(areas["ALAND"], errors="coerce") / 1_000_000
    return land_area.where(land_area.gt(0), geometry_area)

def _intersecting_counties(boundary: gpd.GeoDataFrame, state_fips: str, year: int, fallback: str) -> list[str]:
    """Return counties with material polygon overlap with one study boundary."""
    counties = fetch_county_geometry(state_fips, year)
    metric = pick_metric_crs(boundary); study = boundary.to_crs(metric).geometry.union_all()
    projected = counties.to_crs(metric); overlap = projected.geometry.intersection(study).area
    codes = counties["COUNTYFP"].astype(str).str.zfill(3)
    material = overlap / study.area > .005
    inside = projected.geometry.representative_point().within(study)
    return sorted(codes.loc[material | inside | codes.eq(str(fallback).zfill(3))].unique())


def _fetch_county_part(
    kind: str,
    cache_scope: str,
    state: str,
    county: str,
    year: int,
):
    """Fetch one independently cacheable county source inside its thread."""
    try:
        with scoped_cache(cache_scope):
            if kind == "acs":
                return fetch_acs(
                    state,
                    county,
                    ["total_population", "elderly_population", "poverty", "no_vehicle"],
                    year,
                )
            if kind == "tracts":
                return fetch_tract_geometry(state, county, year)
        raise ValueError(f"Unknown county source: {kind}")
    except Exception as exc:
        raise RuntimeError(
            f"{kind.upper()} acquisition failed for state={state}, county={county}, year={year}: {exc}"
        ) from exc


@registry.register("validate_constraints", "Check confirmed global and regional count constraints before data acquisition.")
@_guard("validate_constraints")
def validate_constraints(ctx: Context) -> ToolResult:
    """Reject arithmetic contradictions and surface unresolved regional geometry."""
    task = ctx.get("task")
    conflicts = [{"region": item.region, "minimum_facilities": item.minimum_facilities,
                  "global_budget": task.n_facilities}
                 for item in task.regional_requirements if item.minimum_facilities > task.n_facilities]
    summary = {"global_budget": task.n_facilities,
               "regional_requirements": [item.model_dump() for item in task.regional_requirements],
               "conflicts": conflicts,
               "requires_region_geometry": bool(task.regional_requirements and not conflicts)}
    ctx.put("constraint_validation", summary)
    if conflicts:
        return ToolResult(ok=False, tool="validate_constraints", handle="constraint_validation", summary=summary,
                          error=ToolError(kind="constraint_conflict",
                                          message="A regional minimum exceeds the confirmed total facility budget.",
                                          hint="Ask the user to change the global budget or regional minimum.", retryable=False))
    return ToolResult(ok=True, tool="validate_constraints", handle="constraint_validation", summary=summary)

@registry.register("resolve_study_area", "Resolve a US place to a boundary and state/county FIPS.",
                   {"place_name": {"type": "string"}})
@_guard("resolve_study_area")
def resolve_study_area(ctx: Context, place_name: str, year: int = 2022) -> ToolResult:
    """Resolve a place boundary and its state/county FIPS identifiers."""
    expect_level = 6 if "county" in place_name.lower() else None
    boundary = resolve_place(place_name, expect_level)
    canonical_name = boundary.attrs.get("canonical_name") or boundary.attrs.get("display_name") or place_name
    with scoped_cache(canonical_name) as cache_scope:
        fips = resolve_fips(canonical_name, boundary, expect_level)
    if boundary.empty: raise ValueError(f"Place not found: {place_name}")
    place_type = boundary.attrs.get("place_type") or boundary.iloc[0].get("addresstype") or (
        "county" if "county" in place_name.lower() else "city")
    area_km2 = boundary.attrs.get("area_km2") or float(boundary.to_crs(boundary.estimate_utm_crs()).area.sum() / 1_000_000)
    if place_type not in {"city", "town", "municipality", "village", "county"}:
        raise ValueError(f"Unsupported study-area type '{place_type}'; use a city or county")
    with scoped_cache(cache_scope):
        county_fips_list = ([fips["county_fips"]] if place_type == "county" else
                            _intersecting_counties(boundary, fips["state_fips"], year, fips["county_fips"]))
    study = {"place_name": canonical_name, "input_place_name": place_name, "year": year,
             "boundary": boundary, "cache_scope": cache_scope, "place_type": place_type,
             "admin_level": boundary.attrs.get("admin_level"), "county_fips_list": county_fips_list,
             "area_km2": area_km2, **fips}
    prov = Provenance(source="OpenStreetMap Nominatim + US Census Geocoder", record_count=len(boundary),
                      notes=f"type={place_type}; area_km2={study['area_km2']:.1f}; cache={cache_scope}")
    ctx.put("study", study, prov)
    return ToolResult(ok=True, tool="resolve_study_area", handle="study", provenance=prov,
                      summary={**fips, "county_fips_list": county_fips_list, "place_type": place_type, "admin_level": study["admin_level"],
                               "area_km2": study["area_km2"], "cache_scope": cache_scope,
                               "canonical_name": canonical_name, "year": year})


@registry.register("prepare_demand", "Fetch and align ACS, TIGER, and Landsat tract demand data.")
@_guard("prepare_demand")
def prepare_demand(ctx: Context, study_handle: str = "study", start_date: str = "2022-06-01",
                   end_date: str = "2022-08-31") -> ToolResult:
    """Build aligned tract demand areas, points, indicators, and lineage."""
    study = ctx.get(study_handle)
    state, year = study["state_fips"], study["year"]
    counties = study.get("county_fips_list", [study["county_fips"]])
    if not counties:
        raise ValueError("No counties resolved for the study area")
    data_cfg = _cfg(ctx, "data")
    ctx.check_cancelled()

    parts = {
        "acs": [None] * len(counties),
        "tracts": [None] * len(counties),
    }
    total_tasks = 2 * len(counties)
    worker_count = min(4, max(1, int(data_cfg.get("io_workers", 4))), total_tasks)
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="county-data",
    ) as executor:
        futures = {
            executor.submit(
                _fetch_county_part,
                kind,
                study["cache_scope"],
                state,
                county,
                year,
            ): (kind, index)
            for index, county in enumerate(counties)
            for kind in ("acs", "tracts")
        }
        completed = 0
        try:
            for future in as_completed(futures):
                kind, index = futures[future]
                parts[kind][index] = future.result()
                completed += 1
                ctx.progress("Census/TIGER download", completed, total_tasks)
        except Exception:
            for future in futures:
                future.cancel()
            raise

    raw_parts = parts["acs"]
    tract_parts = parts["tracts"]
    if any(part is None for part in raw_parts + tract_parts):
        raise RuntimeError("County data acquisition finished incompletely")
    raw = pd.concat(raw_parts, ignore_index=True)
    tracts = gpd.GeoDataFrame(
        pd.concat(tract_parts, ignore_index=True),
        geometry="geometry",
        crs=tract_parts[0].crs,
    )
    demographics = derive_indicators(raw)
    areas = tracts.merge(demographics, on="GEOID", how="inner", validate="one_to_one")
    if len(areas) / max(len(demographics), 1) <= .95: raise ValueError("ACS/TIGER tract join rate is at most 95%")
    if study["place_type"] != "county":
        inside = centroids(areas).geometry.within(study["boundary"].geometry.union_all())
        areas = areas.loc[inside].reset_index(drop=True)
    if areas.empty: raise ValueError("No county tracts have centroids inside the requested boundary")
    try:
        with scoped_cache(study["cache_scope"]):
            heat, heat_prov = fetch_heat_exposure(
                areas[["GEOID", "geometry"]],
                start_date,
                end_date,
                readable_workers=data_cfg.get("landsat_probe_workers", 8),
                dask_workers=data_cfg.get("dask_workers", 4),
                cancel_check=ctx.check_cancelled,
            )
    except RunCancelled:
        raise
    except Exception as exc:
        raise RuntimeError(f"Landsat heat exposure acquisition failed: {exc}") from exc
    ctx.check_cancelled()
    areas = areas.merge(heat, on="GEOID", how="left", validate="one_to_one")
    missing_heat = int(areas["mean_lst"].isna().sum())
    areas["heat_exposure"] = areas["mean_lst"].fillna(areas["mean_lst"].median())
    quality = ctx.get("quality", {})
    quality["heat"] = {"missing_tracts": missing_heat, "total_tracts": len(areas),
                       "imputation": "median" if missing_heat else "none"}
    ctx.put("quality", quality)
    metric_crs = pick_metric_crs(areas)
    areas["population_density"] = areas["total_population"].astype(float) / _land_area_km2(areas).replace(0, np.nan)
    areas = areas.loc[areas["total_population"].fillna(0).gt(0) & areas["elderly_ratio"].notna()].reset_index(drop=True)
    points, population = centroids(areas).reset_index(drop=True), areas["total_population"].to_numpy(float)
    poverty = areas["poverty_rate"].fillna(areas["poverty_rate"].median()).to_numpy(float)
    if "no_vehicle_ratio" not in areas:
        areas["no_vehicle_ratio"] = np.nan
    vehicle_missing = int(areas["no_vehicle_ratio"].isna().sum())
    vehicle_median = areas["no_vehicle_ratio"].median()
    if not np.isfinite(vehicle_median):
        vehicle_median = 0.0
    areas["no_vehicle_ratio"] = areas["no_vehicle_ratio"].fillna(vehicle_median).clip(0, 1)
    quality["mobility"] = {
        "source": "ACS B08201 household vehicle availability",
        "missing_tracts": vehicle_missing,
        "total_tracts": len(areas),
        "imputation": "median" if vehicle_missing else "none",
        "interpretation": "tract-level household proxy, not individual travel behavior",
    }
    indicators = {name: areas[name].fillna(areas[name].median()).to_numpy(float) for name in
                  ["population_density", "elderly_ratio", "poverty_rate", "heat_exposure"]}
    demand = {"areas": areas, "points": points, "population": population,
              "no_vehicle_ratio": areas["no_vehicle_ratio"].to_numpy(float),
              "vulnerable_mask": poverty >= np.quantile(poverty, .75), "indicators": indicators}
    provenance = [Provenance(source=f"US Census ACS {year} 5-year", record_count=len(demographics), crs="none"),
                  Provenance(source=f"US Census TIGER/Line {year}", record_count=len(tracts)), heat_prov]
    ctx.put("demand", demand, provenance)
    return ToolResult(ok=True, tool="prepare_demand", handle="demand", provenance=heat_prov,
                      summary={"tracts": len(areas), "vulnerable_tracts": int(demand["vulnerable_mask"].sum()),
                               "heat_missing_tracts": missing_heat, "heat_imputation": quality["heat"]["imputation"],
                               "vehicle_missing_tracts": vehicle_missing,
                               "mean_no_vehicle_household_ratio": float(areas["no_vehicle_ratio"].mean())})


@registry.register("fetch_candidate_sites", "Fetch OSM candidate facilities inside the study boundary.",
                   {"amenities": {"type": "array", "items": {"type": "string"}}})
@_guard("fetch_candidate_sites")
def fetch_candidate_sites(ctx: Context, amenities: list[str], study_handle: str = "study") -> ToolResult:
    """Fetch candidate facility points for the requested OSM amenities."""
    study = ctx.get(study_handle)
    with scoped_cache(study["cache_scope"]): candidates, error = fetch_candidates(study["boundary"], amenities)
    if error: return ToolResult(ok=False, tool="fetch_candidate_sites", error=error)
    potential = len(candidates)
    candidates, access = _filter_candidate_access(candidates)
    candidates, hours = _filter_candidate_hours(ctx, candidates)
    quality = ctx.get("quality", {})
    quality["public_access"] = access
    ctx.put("quality", quality)
    prov = Provenance(
        source="OpenStreetMap amenities, access, and opening_hours",
        record_count=len(candidates),
        notes=(f"potential={potential}; available={len(candidates)}; "
               f"access_restricted={access['restricted_candidates']}; "
               f"hours_unknown={hours.get('unknown_candidates', 0)}"),
    )
    ctx.put("candidates", candidates, prov)
    return ToolResult(ok=True, tool="fetch_candidate_sites", handle="candidates", provenance=prov,
                      summary={"candidates": len(candidates), "amenities": amenities,
                               "public_access": access, "operating_hours": hours})


@registry.register("load_candidate_sites", "Load user-supplied candidate points from a CSV with latitude and longitude columns.",
                   {"candidate_file": {"type": "string"}})
@_guard("load_candidate_sites")
def load_candidate_sites(ctx: Context, candidate_file: str | None) -> ToolResult:
    """Load and spatially filter user-provided candidate points."""
    if not candidate_file: raise ValueError("A candidate CSV file is required")
    path = Path(candidate_file)
    if not path.is_file(): raise ValueError(f"Candidate CSV not found: {path.name}")
    frame = pd.read_csv(path); columns = {name.lower(): name for name in frame.columns}
    lat = columns.get("latitude", columns.get("lat")); lon = columns.get("longitude", columns.get("lon", columns.get("lng")))
    if not lat or not lon: raise ValueError("Candidate CSV must contain latitude/longitude or lat/lon columns")
    if "cand_id" not in frame: frame["cand_id"] = [f"csv_{index + 1}" for index in range(len(frame))]
    candidates = gpd.GeoDataFrame(frame, geometry=gpd.points_from_xy(frame[lon], frame[lat]), crs="EPSG:4326")
    boundary = ctx.get("study")["boundary"].geometry.union_all()
    candidates = candidates.loc[candidates.geometry.within(boundary) | candidates.geometry.touches(boundary)].reset_index(drop=True)
    if candidates.empty: raise ValueError("No CSV candidate points fall inside the confirmed study area")
    candidates["cand_id"] = candidates["cand_id"].astype(str)
    potential = len(candidates)
    candidates, access = _filter_candidate_access(candidates)
    candidates, hours = _filter_candidate_hours(ctx, candidates)
    quality = ctx.get("quality", {})
    quality["public_access"] = access
    ctx.put("quality", quality)
    prov = Provenance(
        source=f"User candidate CSV: {path.name}",
        record_count=len(candidates),
        notes=(f"potential={potential}; available={len(candidates)}; "
               f"access_restricted={access['restricted_candidates']}; "
               f"hours_unknown={hours.get('unknown_candidates', 0)}"),
    )
    ctx.put("candidates", candidates, prov)
    return ToolResult(ok=True, tool="load_candidate_sites", handle="candidates", provenance=prov,
                      summary={"candidates": len(candidates), "source": path.name,
                               "public_access": access, "operating_hours": hours})


@registry.register("summarize_heat_exposure", "Rank the hottest prepared census tracts without facility optimization.",
                   {"limit": {"type": "integer"}})
@_guard("summarize_heat_exposure")
def summarize_heat_exposure(ctx: Context, limit: int = 10) -> ToolResult:
    """Write a heat map, spatial-analysis chart, ranked tracts, and compact report."""
    areas, study, task = ctx.get("demand")["areas"], ctx.get("study"), ctx.get("task")
    hottest = areas.nlargest(max(1, limit), "heat_exposure")[["GEOID", "heat_exposure"]]
    out = Path(_cfg(ctx, "output").get("dir", "outputs_run")) / study["cache_scope"]
    out.mkdir(parents=True, exist_ok=True)
    map_path, chart_path, report_path = (str(out / "heat_exposure_map.html"),
                                         str(out / "heat_analysis.png"), str(out / "heat_report.md"))
    heat_exposure_map(areas, map_path, study["boundary"])
    statistics = heat_analysis_chart(areas, chart_path)
    summary = {"top_tracts": hottest.to_dict("records"), "tracts_reviewed": len(areas),
               "statistics": statistics, "map": map_path, "analysis_chart": chart_path, "report": report_path}
    Path(report_path).write_text(render_heat_report(task, summary, ctx.lineage), encoding="utf-8")
    ctx.put("heat_summary", summary)
    return ToolResult(ok=True, tool="summarize_heat_exposure", handle="heat_summary", summary=summary)


@registry.register("build_travel_matrix", "Build demand-to-candidate travel minutes with declared routing fallback.")
@_guard("build_travel_matrix")
def build_travel_matrix(ctx: Context, demand_handle: str = "demand",
                        candidates_handle: str = "candidates") -> ToolResult:
    """Build all matrices required by the confirmed mobility scenario."""
    demand, candidates = ctx.get(demand_handle), ctx.get(candidates_handle)
    cfg, study, task = _cfg(ctx, "accessibility"), ctx.get("study", {}), ctx.get("task", None)
    scenario = getattr(task, "mobility_scenario", cfg.get("mobility_scenario", "walking"))
    modes = scenario_modes(scenario)
    speeds = cfg.get("mode_speeds_kmh", {})
    detours = cfg.get("mode_detour_factors", {})
    fallbacks = {
        mode: haversine_matrix(
            demand["points"],
            candidates,
            speeds.get(mode, cfg.get("speed_kmh", 4.8)),
            detours.get(mode, cfg.get("detour_factor", 1.3)),
        )
        for mode in modes
    }
    requested = cfg.get("router") == "ors"
    cache_scope = study.get("cache_scope")
    if requested and not cache_scope:
        raise ValueError("ORS routing requires a concrete study cache scope")

    configured_threshold = float(getattr(task, "threshold_minutes", cfg.get("threshold_minutes", 15.0)))
    sensitivity_thresholds = list(getattr(task, "sensitivity_thresholds", []) or [])
    policy_minutes = max([configured_threshold, *map(float, sensitivity_thresholds)])
    reason = None
    if requested and policy_minutes > cfg.get("ors_max_policy_threshold_minutes", 20):
        reason = f"threshold {policy_minutes:g} minutes exceeds ORS policy limit"
    elif requested and cfg.get("ors_city_only", False) and study.get("place_type", "city") not in {
        "city", "town", "municipality", "village"
    }:
        reason = f"ORS is limited to city boundaries, got {study.get('place_type')}"
    elif requested and study.get("area_km2", 0) > cfg.get("ors_max_area_km2", 15000):
        reason = f"study area {study['area_km2']:.1f} km2 exceeds ORS limit"

    if reason:
        log.warning("ORS routing unavailable (%s); using Haversine", reason)

    matrices: dict[str, np.ndarray] = {}
    mode_reports: dict[str, dict] = {}
    if not requested or reason:
        for mode in modes:
            matrices[mode] = fallbacks[mode]
            mode_reports[mode] = {
                "router": "haversine",
                "tier": "fallback" if reason else "approximate",
                "reason": reason or "quick preview",
                "total_pairs": int(fallbacks[mode].size),
            }
    else:
        upper_speeds = cfg.get("ors_upper_speeds_kmh", {})
        with scoped_cache(cache_scope):
            for mode in modes:
                lower_bound = haversine_matrix(
                    demand["points"],
                    candidates,
                    upper_speeds.get(mode, cfg.get("ors_upper_speed_kmh", 6.0)),
                    1.0,
                )
                eligible = lower_bound <= policy_minutes * cfg.get("ors_pruning_margin", 1.5)
                eligible_pairs = int(eligible.sum())
                if eligible_pairs > cfg.get("ors_max_eligible_pairs", 250000):
                    matrices[mode] = fallbacks[mode]
                    mode_reports[mode] = {
                        "router": "haversine",
                        "tier": "fallback",
                        "eligible_pairs": eligible_pairs,
                        "reason": "eligible pairs exceed ORS limit",
                    }
                    continue

                def progress(label, done, total, **stats):
                    ctx.progress(f"{mode}: {label}", done, total, **stats)

                matrices[mode], mode_reports[mode] = sparse_network_matrix(
                    demand["points"],
                    candidates,
                    eligible,
                    fallback_matrix=fallbacks[mode],
                    mode=mode,
                    base_url=cfg.get("ors_base_url"),
                    source_chunk_size=cfg.get("ors_source_chunk_size", 35),
                    destination_chunk_size=cfg.get("ors_destination_chunk_size", 50),
                    tile_size_m=cfg.get("ors_tile_size_m", 3000),
                    max_routes_per_request=cfg.get("ors_max_routes_per_request", 3500),
                    max_concurrency=cfg.get("ors_max_concurrency", 2),
                    max_inflight_routes=cfg.get("ors_max_inflight_routes", 3500),
                    max_requests=cfg.get("ors_max_requests", 500),
                    request_interval_s=cfg.get("ors_request_interval_s", 1.6),
                    cache_version=cfg.get("ors_cache_version", "v2"),
                    cache_ttl_days=cfg.get("ors_matrix_ttl_days", 30),
                    cache_max_gb=cfg.get("ors_cache_max_gb", 2),
                    timeout_s=cfg.get("ors_timeout_s", 60),
                    progress_callback=progress,
                    cancel_check=ctx.check_cancelled,
                )

    precise = all(str(report.get("router", "")).startswith("ors")
                  for report in mode_reports.values())
    router = (
        ("ors_multimodal" if len(modes) > 1 else next(iter(mode_reports.values()))["router"])
        if precise else
        ("haversine_multimodal" if len(modes) > 1 else "haversine")
    )
    if cfg.get("strict_routing") and not precise:
        failed = {
            mode: report.get("reason", "network routing unavailable")
            for mode, report in mode_reports.items()
            if not str(report.get("router", "")).startswith("ors")
        }
        raise RuntimeError(f"Precise mode requires ORS for every mobility profile: {failed}")

    composed = compose_demand(demand, matrices, scenario)
    matrix = composed["matrix"]
    diagnostics = validate_travel_matrix(matrix, matrix.shape[0], len(candidates))
    total = lambda key: sum(int(report.get(key, 0) or 0) for report in mode_reports.values())
    routing_report = {
        "router": router,
        "tier": "cache" if mode_reports and all(
            report.get("tier") == "cache" for report in mode_reports.values()
        ) else "live" if precise else "fallback" if reason else "approximate",
        "mobility_scenario": scenario,
        "modes": list(modes),
        "mode_reports": mode_reports,
        "mode_population_share": composed["mode_population_share"],
        "operating_hours": ctx.get("quality", {}).get("operating_hours", {}),
        "cache_pairs": total("cache_pairs"),
        "live_pairs": total("live_pairs"),
        "eligible_pairs": total("eligible_pairs"),
        "planned_requests": total("planned_requests"),
        "completed_requests": total("completed_requests"),
    }
    if reason:
        routing_report["reason"] = reason
    travel = {
        **composed,
        "mode_matrices": matrices,
        "router": router,
        "diagnostics": diagnostics,
        "routing_report": routing_report,
    }
    provenance = Provenance(
        source=f"{router} travel-time matrices",
        tier="cache" if routing_report["tier"] == "cache" else "live",
        record_count=matrix.size,
        crs="none",
        notes=(
            f"mobility={scenario}; modes={','.join(modes)}; "
            f"group shares are ACS B08201 tract-level household proxies"
            if scenario == "walk_drive_equity"
            else f"mobility={scenario}; modes={','.join(modes)}"
        ),
    )
    ctx.put("travel", travel, provenance)
    return ToolResult(
        ok=True,
        tool="build_travel_matrix",
        handle="travel",
        provenance=provenance,
        summary={**routing_report, **diagnostics},
    )

@registry.register("optimize_sites", "Maximize coverage for a fixed facility budget and threshold.",
                   {"n_facilities": {"type": "integer"}, "threshold_minutes": {"type": "number"},
                    "weights": {"type": ["object", "null"]}, "coverage_target": {"type": ["number", "null"]},
                    "coverage_group": {"type": "string", "enum": ["all", "vulnerable"]}})
@_guard("optimize_sites")
def optimize_sites(ctx: Context, n_facilities: int = 8, threshold_minutes: float = 15,
                   weights: dict | ValueWeights | None = None, coverage_target: float | None = None,
                   coverage_group: str = "all") -> ToolResult:
    """Solve the requested profile with any confirmed tract-coverage constraint."""
    demand, candidates, travel = ctx.get("demand"), ctx.get("candidates"), ctx.get("travel")
    matrix, population, vulnerable, indicators = _decision_inputs(demand, travel)
    candidate_ids = candidates["cand_id"].astype(str).tolist(); candidate_count = len(candidate_ids)
    target_mask = _coverage_mask(demand, coverage_group, travel)
    if n_facilities > candidate_count:
        summary = {"requested_facilities": n_facilities, "candidate_count": candidate_count,
                   "coverage_target": coverage_target}
        ctx.put("constraint_diagnostics", summary)
        return ToolResult(ok=False, tool="optimize_sites", summary=summary,
                          error=ToolError(kind="constraint_conflict",
                                          message=f"Requested {n_facilities} facilities but only {candidate_count} candidates exist.",
                                          hint="Upload or query more candidates, or reduce the facility count.", retryable=False))
    optimization_cfg = _cfg(ctx, "optimization")
    checkpoint_key = (
        id(matrix),
        id(population),
        id(vulnerable),
        n_facilities,
        float(threshold_minutes),
        11,
    )
    checkpoints = ctx.get("_pareto_checkpoints", {})
    completed_points = checkpoints.setdefault(checkpoint_key, {})
    ctx.put("_pareto_checkpoints", checkpoints)

    def checkpoint(index, point):
        completed_points[index] = point

    solver_time_limit = optimization_cfg.get("solver_time_limit_s", 120)

    def pareto_progress(label, done, total):
        ctx.progress(label, done, total, solve_time_limit_seconds=solver_time_limit)

    pareto_progress("Pareto sweep", 0, 11)
    points = coverage_pareto_sweep(
        matrix,
        population,
        vulnerable,
        candidate_ids,
        n_facilities,
        threshold_minutes,
        progress_callback=pareto_progress,
        max_workers=optimization_cfg.get("pareto_workers", 4),
        time_limit_s=solver_time_limit,
        cancel_check=ctx.check_cancelled,
        checkpoint_callback=checkpoint,
        completed_points=completed_points,
    )
    value_weights = weights if isinstance(weights, ValueWeights) else (ValueWeights(**weights) if weights else None)
    if value_weights is None and coverage_target is None:
        solution = points[0].solution
    else:
        objective = (build_demand_weights(indicators, population, value_weights)
                     if value_weights else population / max(population.sum(), 1))
        ctx.progress("Requested MCLP", 0, 1)
        solution = solve_mclp(matrix, objective, population, vulnerable,
                              n_facilities, threshold_minutes, candidate_ids, "requested", value_weights,
                              optimization_cfg.get("solver_time_limit_s", 120),
                              coverage_target=coverage_target, coverage_mask=target_mask)
        ctx.progress("Requested MCLP", 1, 1)
    reachable = matrix <= threshold_minutes
    max_tract_coverage = float(reachable[target_mask].any(axis=1).mean()) if target_mask.any() else 0.0
    nearest = np.min(matrix, axis=1) if len(reachable) else np.array([])
    target_nearest = nearest[target_mask] if len(nearest) else np.array([])
    minimum_full_threshold = float(target_nearest.max()) if len(target_nearest) and np.isfinite(target_nearest).all() else None
    diagnostics = {"requested_facilities": n_facilities, "candidate_count": candidate_count,
                   "coverage_target": coverage_target, "coverage_group": coverage_group,
                   "max_tract_coverage_at_threshold": max_tract_coverage,
                   "minimum_threshold_with_all_candidates": minimum_full_threshold}
    if solution.solver_status not in {"Optimal", "Feasible"}:
        ctx.put("constraint_diagnostics", diagnostics)
        if coverage_target is not None and max_tract_coverage + 1e-9 < coverage_target:
            hint = "Even all current candidates cannot meet the target at this threshold; increase the threshold or expand candidates."
        else: hint = "Increase the facility budget or threshold, or relax the coverage target."
        analysis = {"solution": solution, "points": points, "frontier": non_dominated(points),
                    "n_facilities": n_facilities, "threshold_minutes": threshold_minutes,
                    "coverage_target": coverage_target, "diagnostics": diagnostics}
        ctx.put("analysis", analysis)
        return ToolResult(ok=False, tool="optimize_sites", summary=diagnostics,
                          error=ToolError(kind="constraint_conflict",
                                          message=f"The confirmed constraints are {solution.solver_status.lower()} under the current candidates and travel matrix.",
                                          hint=hint, retryable=False))
    selected = {candidate_id: index for index, candidate_id in enumerate(candidate_ids)}
    columns = [selected[candidate_id] for candidate_id in solution.selected_ids if candidate_id in selected]
    covered = np.any(matrix[:, columns] <= threshold_minutes, axis=1) if columns else np.zeros(len(population), bool)
    covered_tract_ratio = float(covered[target_mask].mean()) if target_mask.any() else 0.0
    all_tract_ratio = float(covered.mean()) if len(covered) else 0.0
    analysis = {"solution": solution, "points": points, "frontier": non_dominated(points),
                "n_facilities": n_facilities, "threshold_minutes": threshold_minutes,
                "coverage_target": coverage_target, "coverage_group": coverage_group,
                "covered_tract_ratio": covered_tract_ratio, "all_tract_ratio": all_tract_ratio,
                "diagnostics": diagnostics}
    ctx.put("analysis", analysis)
    scenario = {"n_facilities": n_facilities, "threshold_minutes": threshold_minutes,
                "coverage_target": coverage_target, "coverage_group": coverage_group,
                "candidate_count": candidate_count, "selected_count": len(solution.selected_ids),
                "covered_population_ratio": solution.covered_ratio,
                "vulnerable_covered_ratio": solution.vulnerable_covered_ratio,
                "covered_tract_ratio": covered_tract_ratio, "all_tract_ratio": all_tract_ratio}
    scenarios = list(ctx.get("scenario_results", [])); scenarios.append(scenario); ctx.put("scenario_results", scenarios)
    chosen = candidates[candidates["cand_id"].astype(str).isin(map(str, solution.selected_ids))]
    selected_sites = [
        {"cand_id": str(row["cand_id"]), "name": _candidate_label(row)}
        for _, row in chosen.iterrows()
    ]
    return ToolResult(ok=True, tool="optimize_sites", handle="analysis",
                      summary={"selected_ids": solution.selected_ids, "selected_sites": selected_sites,
                               "covered_ratio": solution.covered_ratio,
                               "vulnerable_covered_ratio": solution.vulnerable_covered_ratio,
                               "covered_tract_ratio": covered_tract_ratio, "all_tract_ratio": all_tract_ratio,
                               "frontier_points": len(analysis["frontier"]), **diagnostics})

@registry.register("find_minimum_facilities", "Use LSCP to find the minimum facility count for a fixed threshold and coverage target.",
                   {"threshold_minutes": {"type": "number"}, "coverage_target": {"type": "number"},
                    "coverage_group": {"type": "string", "enum": ["all", "vulnerable"]}})
@_guard("find_minimum_facilities")
def find_minimum_facilities(ctx: Context, threshold_minutes: float = 15,
                            coverage_target: float = 1.0, coverage_group: str = "all") -> ToolResult:
    """Run LSCP and report the exact facility lower bound at a fixed threshold."""
    demand, candidates, travel = ctx.get("demand"), ctx.get("candidates"), ctx.get("travel")
    matrix, population, vulnerable, _ = _decision_inputs(demand, travel)
    candidate_ids = candidates["cand_id"].astype(str).tolist(); mask = _coverage_mask(demand, coverage_group, travel)
    ctx.progress("LSCP", 0, 1)
    solution = solve_lscp(matrix, population, vulnerable,
                          threshold_minutes, candidate_ids, coverage_target, mask,
                          _cfg(ctx, "optimization").get("solver_time_limit_s", 120))
    ctx.progress("LSCP", 1, 1)
    feasible = solution.solver_status in {"Optimal", "Feasible"}
    current = ctx.get("task", None); minimum = len(solution.selected_ids) if feasible else None
    summary = {"feasible": feasible, "minimum_facilities": minimum,
               "current_facilities": getattr(current, "n_facilities", None),
               "facility_gap": (minimum - current.n_facilities if feasible and current else None),
               "threshold_minutes": threshold_minutes, "coverage_target": coverage_target,
               "coverage_group": coverage_group, "solver_status": solution.solver_status,
               "selected_ids": solution.selected_ids}
    study = ctx.get("study", {})
    if feasible and study and "areas" in demand:
        out = Path(_cfg(ctx, "output").get("dir", "outputs_run")) / study["cache_scope"]
        out.mkdir(parents=True, exist_ok=True); summary["map"] = str(out / "minimum_facilities_map.html")
        coverage_map(
            demand["areas"], candidates, solution, "heat_exposure", summary["map"],
            study.get("boundary"), task=ctx.get("task", None),
        )
    ctx.put("minimum_facilities", {"solution": solution, **summary})
    return ToolResult(ok=True, tool="find_minimum_facilities", handle="minimum_facilities", summary=summary)


@registry.register("find_minimum_threshold", "Find the smallest matrix-supported threshold for a fixed facility budget and coverage target.",
                   {"n_facilities": {"type": "integer"}, "coverage_target": {"type": "number"},
                    "coverage_group": {"type": "string", "enum": ["all", "vulnerable"]}})
@_guard("find_minimum_threshold")
def find_minimum_threshold(ctx: Context, n_facilities: int = 8,
                           coverage_target: float = 1.0, coverage_group: str = "all") -> ToolResult:
    """Search exact observed travel times for the minimum feasible threshold."""
    demand, candidates, travel = ctx.get("demand"), ctx.get("candidates"), ctx.get("travel")
    matrix, population, vulnerable, _ = _decision_inputs(demand, travel)
    candidate_ids = candidates["cand_id"].astype(str).tolist(); mask = _coverage_mask(demand, coverage_group, travel)
    threshold, solution = minimum_coverage_threshold(
        matrix, population, vulnerable, candidate_ids,
        n_facilities, coverage_target, mask, _cfg(ctx, "optimization").get("solver_time_limit_s", 120), ctx.progress)
    current = ctx.get("task", None)
    summary = {"feasible": threshold is not None, "minimum_threshold_minutes": threshold,
               "current_threshold_minutes": getattr(current, "threshold_minutes", None),
               "threshold_gap_minutes": (threshold - current.threshold_minutes if threshold is not None and current else None),
               "n_facilities": n_facilities, "coverage_target": coverage_target,
               "coverage_group": coverage_group,
               "selected_ids": solution.selected_ids if solution else []}
    study = ctx.get("study", {})
    if solution and threshold is not None and study and "areas" in demand:
        out = Path(_cfg(ctx, "output").get("dir", "outputs_run")) / study["cache_scope"]
        out.mkdir(parents=True, exist_ok=True); summary["map"] = str(out / "minimum_threshold_map.html")
        coverage_map(
            demand["areas"], candidates, solution, "heat_exposure", summary["map"],
            study.get("boundary"), task=ctx.get("task", None),
        )
    ctx.put("minimum_threshold", {"solution": solution, **summary})
    return ToolResult(ok=True, tool="find_minimum_threshold", handle="minimum_threshold", summary=summary)


@registry.register("analyze_reachability", "Rank hard-to-serve tracts and report structural coverage limits from the travel matrix.",
                   {"threshold_minutes": {"type": "number"},
                    "coverage_group": {"type": "string", "enum": ["all", "vulnerable"]},
                    "limit": {"type": "integer"}})
@_guard("analyze_reachability")
def analyze_reachability(ctx: Context, threshold_minutes: float = 15,
                         coverage_group: str = "all", limit: int = 10) -> ToolResult:
    """Summarize nearest candidates, unserved demand, and hardest tracts."""
    demand, travel = ctx.get("demand"), ctx.get("travel"); matrix, _, _, _ = _decision_inputs(demand, travel)
    mask = _coverage_mask(demand, coverage_group, travel); nearest = matrix.min(axis=1)
    reachable_count = (matrix <= threshold_minutes).sum(axis=1)
    tract_ids = (demand["areas"]["GEOID"].astype(str).tolist() if "GEOID" in demand["areas"]
                 else [str(index) for index in range(len(demand["population"]))])
    base_index = np.asarray(travel.get("base_index", np.arange(len(matrix))), dtype=int)
    modes = np.asarray(travel.get("mode", ["unknown"] * len(matrix)), dtype=object)
    grouped = len(set(modes.tolist())) > 1
    ids = [
        f"{tract_ids[base]}:{modes[index]}" if grouped else tract_ids[base]
        for index, base in enumerate(base_index)
    ]
    rows = [{"demand_id": ids[index],
             "nearest_minutes": None if not np.isfinite(nearest[index]) else float(nearest[index]),
             "reachable_candidates": int(reachable_count[index])}
            for index in np.flatnonzero(mask)]
    rows.sort(key=lambda row: float("inf") if row["nearest_minutes"] is None else row["nearest_minutes"], reverse=True)
    summary = {"coverage_group": coverage_group, "threshold_minutes": threshold_minutes,
               "maximum_tract_coverage": float((reachable_count[mask] > 0).mean()),
               "unserved_demand_ids": [ids[index] for index in np.flatnonzero(mask & (reachable_count == 0))],
               "structurally_unreachable_ids": [ids[index] for index in np.flatnonzero(mask & ~np.isfinite(nearest))],
               "hardest_demand": rows[:max(1, limit)]}
    ctx.put("reachability_analysis", summary)
    return ToolResult(ok=True, tool="analyze_reachability", handle="reachability_analysis", summary=summary)


@registry.register(
    "run_sensitivity_analysis",
    "Solve a confirmed grid of facility counts and access thresholds against the existing travel matrix.",
    {"threshold_minutes": {"type": "array", "items": {"type": "number"}},
     "facility_counts": {"type": "array", "items": {"type": "integer"}},
     "weights": {"type": ["object", "null"]},
     "coverage_group": {"type": "string", "enum": ["all", "vulnerable"]}},
)
@_guard("run_sensitivity_analysis")
def run_sensitivity_analysis(
    ctx: Context,
    threshold_minutes: list[float],
    facility_counts: list[int],
    weights: dict | ValueWeights | None = None,
    coverage_group: str = "all",
) -> ToolResult:
    """Run a bounded, deterministic what-if grid without changing the confirmed baseline."""
    thresholds = list(dict.fromkeys(float(value) for value in threshold_minutes))
    counts = list(dict.fromkeys(int(value) for value in facility_counts))
    if not thresholds or not counts:
        raise ValueError("At least one confirmed threshold and facility count are required")
    if any(value <= 0 for value in thresholds):
        raise ValueError("Sensitivity thresholds must be positive")
    if len(thresholds) * len(counts) > 36:
        raise ValueError("Sensitivity analysis is limited to 36 confirmed experiments")

    demand, candidates, travel = ctx.get("demand"), ctx.get("candidates"), ctx.get("travel")
    matrix, population, vulnerable, indicators = _decision_inputs(demand, travel)
    candidate_ids = candidates["cand_id"].astype(str).tolist()
    if any(value < 1 or value > len(candidate_ids) for value in counts):
        raise ValueError(f"Facility counts must be between 1 and {len(candidate_ids)}")
    task = ctx.get("task", None)
    routed_limit = max(
        [float(getattr(task, "threshold_minutes", max(thresholds))),
         *map(float, getattr(task, "sensitivity_thresholds", []) or [])]
    )
    if max(thresholds) > routed_limit + 1e-9:
        raise ValueError(
            "A sensitivity threshold exceeds the confirmed routed range; revise the task "
            "so the travel matrix is rebuilt for that threshold"
        )

    value_weights = weights if isinstance(weights, ValueWeights) else (
        ValueWeights(**weights) if weights else None
    )
    objective = (
        build_demand_weights(indicators, population, value_weights)
        if value_weights else population / max(population.sum(), 1)
    )
    target_mask = _coverage_mask(demand, coverage_group, travel)
    experiments = [(count, threshold) for count in counts for threshold in thresholds]
    results = []
    time_limit = _cfg(ctx, "optimization").get("solver_time_limit_s", 120)
    for index, (count, threshold) in enumerate(experiments):
        ctx.check_cancelled()
        ctx.progress("Sensitivity experiments", index, len(experiments))
        solution = solve_mclp(
            matrix, objective, population, vulnerable, count, threshold,
            candidate_ids, "sensitivity", value_weights, time_limit,
        )
        selected = {candidate_id: position for position, candidate_id in enumerate(candidate_ids)}
        columns = [selected[item] for item in solution.selected_ids if item in selected]
        covered = (
            np.any(matrix[:, columns] <= threshold, axis=1)
            if columns else np.zeros(len(population), dtype=bool)
        )
        results.append({
            "n_facilities": count,
            "threshold_minutes": threshold,
            "solver_status": solution.solver_status,
            "selected_ids": solution.selected_ids,
            "covered_population_ratio": solution.covered_ratio,
            "vulnerable_covered_ratio": solution.vulnerable_covered_ratio,
            "covered_tract_ratio": float(covered[target_mask].mean()) if target_mask.any() else 0.0,
            "mean_travel_minutes": solution.mean_travel_minutes,
        })
    ctx.progress("Sensitivity experiments", len(experiments), len(experiments))
    baseline = results[0]
    for result in results:
        result["delta_coverage"] = (
            result["covered_population_ratio"] - baseline["covered_population_ratio"]
        )
        result["delta_vulnerable_coverage"] = (
            result["vulnerable_covered_ratio"] - baseline["vulnerable_covered_ratio"]
        )
    summary = {
        "baseline": baseline,
        "experiments": results,
        "experiment_count": len(results),
        "coverage_group": coverage_group,
        "router": travel.get("router", "unknown"),
        "mobility_scenario": getattr(task, "mobility_scenario", "unknown"),
    }
    ctx.put("sensitivity_results", summary)
    return ToolResult(
        ok=True, tool="run_sensitivity_analysis", handle="sensitivity_results", summary=summary
    )


@registry.register("compare_scenarios", "Compare the two most recent successful optimization scenarios without rerunning a solver.")
@_guard("compare_scenarios")
def compare_scenarios(ctx: Context) -> ToolResult:
    """Return parameter and outcome deltas between the last two saved scenarios."""
    scenarios = ctx.get("scenario_results", [])
    if len(scenarios) < 2: raise ValueError("Two successful optimization scenarios are required for comparison")
    baseline, alternative = scenarios[-2], scenarios[-1]
    numeric = ["n_facilities", "threshold_minutes", "candidate_count", "selected_count",
               "covered_population_ratio", "vulnerable_covered_ratio", "covered_tract_ratio", "all_tract_ratio"]
    delta = {key: alternative[key] - baseline[key] for key in numeric
             if isinstance(baseline.get(key), (int, float)) and isinstance(alternative.get(key), (int, float))}
    summary = {"baseline": baseline, "alternative": alternative, "delta": delta}
    ctx.put("scenario_comparison", summary)
    return ToolResult(ok=True, tool="compare_scenarios", handle="scenario_comparison", summary=summary)

@registry.register("generate_outputs", "Write coverage HTML, Pareto PNG, and Markdown report.")
@_guard("generate_outputs")
def generate_outputs(ctx: Context, output_dir: str | None = None, indicator: str = "heat_exposure") -> ToolResult:
    """Write the coverage map, Pareto chart, and Markdown report."""
    study, demand, candidates, analysis = ctx.get("study"), ctx.get("demand"), ctx.get("candidates"), ctx.get("analysis")
    task = ctx.get("task", TaskSpec(place_name=study["place_name"], n_facilities=analysis["n_facilities"],
                                    threshold_minutes=analysis["threshold_minutes"], weights=analysis["solution"].weights_used))
    base = Path(output_dir or _cfg(ctx, "output").get("dir", "outputs_run"))
    out = base if output_dir else base / study["cache_scope"]; out.mkdir(parents=True, exist_ok=True)
    paths = {"map": str(out / "coverage_map.html"), "pareto": str(out / "pareto.png"), "report": str(out / "report.md")}
    coverage_map(
        demand["areas"], candidates, analysis["solution"], indicator, paths["map"],
        study["boundary"], task=task,
    )
    pareto_chart(analysis["points"], analysis["frontier"], paths["pareto"])
    Path(paths["report"]).write_text(render_report(task, analysis["solution"], analysis["frontier"], ctx.lineage), encoding="utf-8")
    ctx.put("outputs", paths)
    return ToolResult(ok=True, tool="generate_outputs", handle="outputs", summary=paths)


def run_tool_chain(cfg: dict, task: TaskSpec, output_dir: str | None = None, ctx: Context | None = None) -> dict:
    """Execute the exact six-tool baseline without an LLM."""
    ctx = ctx or Context(); ctx.put("config", cfg); ctx.put("task", task)
    calls = [
        ("resolve_study_area", {"place_name": task.place_name}),
        ("prepare_demand", {}),
        ("fetch_candidate_sites", {"amenities": task.candidate_amenities}),
        ("build_travel_matrix", {}),
        ("optimize_sites", {"n_facilities": task.n_facilities, "threshold_minutes": task.threshold_minutes,
                            "weights": task.weights.model_dump() if task.weights else None,
                            "coverage_target": task.coverage_target, "coverage_group": task.coverage_group}),
        ("generate_outputs", {"output_dir": output_dir}),
    ]
    results = []
    for name, args in calls:
        result = registry.get(name).fn(ctx=ctx, **args); results.append(result)
        if not result.ok: return {"ok": False, "failed_tool": name, "results": results, "context": ctx}
    return {"ok": True, "outputs": ctx.get("outputs"), "solution": ctx.get("analysis")["solution"],
            "results": results, "context": ctx}
