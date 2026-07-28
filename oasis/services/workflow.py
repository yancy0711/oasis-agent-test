"""Coarse baseline and diagnostic tools shared by the pipeline and agent."""
from __future__ import annotations

import logging
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
from oasis.core.data.census import derive_indicators, fetch_acs, fetch_county_geometry, fetch_tract_geometry
from oasis.core.data.geocoding import AmbiguousPlaceError, resolve_fips, resolve_place
from oasis.core.data.heat import fetch_heat_exposure
from oasis.core.data.osm import fetch_candidates
from oasis.core.optimization.models import (build_demand_weights, minimum_coverage_threshold,
                                       solve_lscp, solve_mclp)
from oasis.core.optimization.pareto import coverage_pareto_sweep, non_dominated
from oasis.services.registry import registry
from oasis.core.spatial.accessibility import (haversine_matrix, sparse_network_matrix,
                                         travel_time_matrix, validate_travel_matrix)
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


def _coverage_mask(demand: dict, group: str) -> np.ndarray:
    """Return the deterministic demand subset named by a public tool argument."""
    if group == "all": return np.ones(len(demand["population"]), dtype=bool)
    if group == "vulnerable": return np.asarray(demand["vulnerable_mask"], dtype=bool)
    raise ValueError(f"Unsupported demand group: {group}")


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
    study = ctx.get(study_handle); state, year = study["state_fips"], study["year"]
    counties = study.get("county_fips_list", [study["county_fips"]])
    with scoped_cache(study["cache_scope"]):
        raw_parts = [fetch_acs(state, county, ["total_population", "elderly_population", "poverty"], year) for county in counties]
        tract_parts = [fetch_tract_geometry(state, county, year) for county in counties]
    raw = pd.concat(raw_parts, ignore_index=True)
    tracts = gpd.GeoDataFrame(pd.concat(tract_parts, ignore_index=True), geometry="geometry", crs=tract_parts[0].crs)
    demographics = derive_indicators(raw)
    areas = tracts.merge(demographics, on="GEOID", how="inner", validate="one_to_one")
    if len(areas) / max(len(demographics), 1) <= .95: raise ValueError("ACS/TIGER tract join rate is at most 95%")
    if study["place_type"] != "county":
        inside = centroids(areas).geometry.within(study["boundary"].geometry.union_all())
        areas = areas.loc[inside].reset_index(drop=True)
    if areas.empty: raise ValueError("No county tracts have centroids inside the requested boundary")
    with scoped_cache(study["cache_scope"]):
        heat, heat_prov = fetch_heat_exposure(areas[["GEOID", "geometry"]], start_date, end_date)
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
    indicators = {name: areas[name].fillna(areas[name].median()).to_numpy(float) for name in
                  ["population_density", "elderly_ratio", "poverty_rate", "heat_exposure"]}
    demand = {"areas": areas, "points": points, "population": population,
              "vulnerable_mask": poverty >= np.quantile(poverty, .75), "indicators": indicators}
    provenance = [Provenance(source=f"US Census ACS {year} 5-year", record_count=len(demographics), crs="none"),
                  Provenance(source=f"US Census TIGER/Line {year}", record_count=len(tracts)), heat_prov]
    ctx.put("demand", demand, provenance)
    return ToolResult(ok=True, tool="prepare_demand", handle="demand", provenance=heat_prov,
                      summary={"tracts": len(areas), "vulnerable_tracts": int(demand["vulnerable_mask"].sum()),
                               "heat_missing_tracts": missing_heat, "heat_imputation": quality["heat"]["imputation"]})


@registry.register("fetch_candidate_sites", "Fetch OSM candidate facilities inside the study boundary.",
                   {"amenities": {"type": "array", "items": {"type": "string"}}})
@_guard("fetch_candidate_sites")
def fetch_candidate_sites(ctx: Context, amenities: list[str], study_handle: str = "study") -> ToolResult:
    """Fetch candidate facility points for the requested OSM amenities."""
    study = ctx.get(study_handle)
    with scoped_cache(study["cache_scope"]): candidates, error = fetch_candidates(study["boundary"], amenities)
    if error: return ToolResult(ok=False, tool="fetch_candidate_sites", error=error)
    prov = Provenance(source="OpenStreetMap amenities", record_count=len(candidates))
    ctx.put("candidates", candidates, prov)
    return ToolResult(ok=True, tool="fetch_candidate_sites", handle="candidates", provenance=prov,
                      summary={"candidates": len(candidates), "amenities": amenities})


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
    prov = Provenance(source=f"User candidate CSV: {path.name}", record_count=len(candidates))
    ctx.put("candidates", candidates, prov)
    return ToolResult(ok=True, tool="load_candidate_sites", handle="candidates", provenance=prov,
                      summary={"candidates": len(candidates), "source": path.name})


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
    """Build and validate the demand-to-candidate travel-time matrix."""
    demand, candidates = ctx.get(demand_handle), ctx.get(candidates_handle)
    cfg, study = _cfg(ctx, "accessibility"), ctx.get("study", {})
    routing_report, requested = {}, cfg.get("router") == "ors"
    cache_scope = study.get("cache_scope")
    if requested and not cache_scope: raise ValueError("ORS routing requires a concrete study cache scope")
    fallback = haversine_matrix(demand["points"], candidates, cfg.get("speed_kmh", 4.8),
                                cfg.get("detour_factor", 1.3)) if requested else None
    task = ctx.get("task", None)
    policy_minutes = getattr(task, "threshold_minutes", cfg.get("threshold_minutes", 15))
    reason = None
    if requested and policy_minutes > cfg.get("ors_max_policy_threshold_minutes", 20):
        reason = f"threshold {policy_minutes:g} minutes exceeds ORS policy limit"
    elif requested and cfg.get("ors_city_only", False) and study.get("place_type", "city") not in {"city", "town", "municipality", "village"}:
        reason = f"ORS is limited to city boundaries, got {study.get('place_type')}"
    elif requested and study.get("area_km2", 0) > cfg.get("ors_max_area_km2", 15000):
        reason = f"study area {study['area_km2']:.1f} km2 exceeds ORS limit"
    if reason:
        matrix, router = fallback, "haversine"
        routing_report = {"router": router, "tier": "fallback", "reason": reason}
        log.warning("ORS skipped; using Haversine: %s", reason)
    elif requested and cfg.get("ors_sparse", True):
        lower_bound = haversine_matrix(demand["points"], candidates,
                                       cfg.get("ors_upper_speed_kmh", 6.0), 1.0)
        horizon = policy_minutes * cfg.get("ors_pruning_margin", 1.5)
        eligible = lower_bound <= horizon
        log.info("ORS scope=%s; threshold=%g min; eligible_pairs=%d", cache_scope,
                 policy_minutes, int(eligible.sum()))
        if int(eligible.sum()) > cfg.get("ors_max_eligible_pairs", 250000):
            matrix, router = fallback, "haversine"
            routing_report = {"router": router, "tier": "fallback",
                              "eligible_pairs": int(eligible.sum()), "reason": "eligible pairs exceed ORS limit"}
            log.warning("ORS skipped; using Haversine: %s", routing_report["reason"])
        else:
            with scoped_cache(cache_scope):
                matrix, routing_report = sparse_network_matrix(
                    demand["points"], candidates, eligible, fallback_matrix=fallback,
                    mode=cfg.get("mode", "walking"), base_url=cfg.get("ors_base_url"),
                    source_chunk_size=cfg.get("ors_source_chunk_size", 25),
                    tile_size_m=cfg.get("ors_tile_size_m", 3000),
                    max_routes_per_request=cfg.get("ors_max_routes_per_request", 2500),
                    max_requests=cfg.get("ors_max_requests", 500),
                    request_interval_s=cfg.get("ors_request_interval_s", 1.6),
                    timeout_s=cfg.get("ors_timeout_s", 60), progress_callback=ctx.progress)
            router = routing_report["router"]
    else:
        if cache_scope:
            with scoped_cache(cache_scope):
                matrix, router = travel_time_matrix(demand["points"], candidates, cfg)
        else:
            matrix, router = travel_time_matrix(demand["points"], candidates, cfg)
    diagnostics = validate_travel_matrix(matrix, len(demand["points"]), len(candidates))
    travel = {"matrix": matrix, "router": router, "diagnostics": diagnostics, "routing_report": routing_report}
    prov = Provenance(source=f"{router} travel-time matrix",
                      tier="cache" if routing_report.get("tier") == "cache" else "live",
                      record_count=matrix.size, crs="none", notes=f"tier={routing_report.get('tier', 'computed')}; {routing_report.get('reason', '')}".strip())
    ctx.put("travel", travel, prov)
    return ToolResult(ok=True, tool="build_travel_matrix", handle="travel", provenance=prov,
                      summary={"router": router, **routing_report, **diagnostics})


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
    candidate_ids = candidates["cand_id"].astype(str).tolist(); candidate_count = len(candidate_ids)
    target_mask = _coverage_mask(demand, coverage_group)
    if n_facilities > candidate_count:
        summary = {"requested_facilities": n_facilities, "candidate_count": candidate_count,
                   "coverage_target": coverage_target}
        ctx.put("constraint_diagnostics", summary)
        return ToolResult(ok=False, tool="optimize_sites", summary=summary,
                          error=ToolError(kind="constraint_conflict",
                                          message=f"Requested {n_facilities} facilities but only {candidate_count} candidates exist.",
                                          hint="Upload or query more candidates, or reduce the facility count.", retryable=False))
    ctx.progress("Pareto sweep", 0, 11)
    points = coverage_pareto_sweep(travel["matrix"], demand["population"], demand["vulnerable_mask"],
                                   candidate_ids, n_facilities, threshold_minutes, progress_callback=ctx.progress)
    value_weights = weights if isinstance(weights, ValueWeights) else (ValueWeights(**weights) if weights else None)
    if value_weights is None and coverage_target is None: solution = points[0].solution
    else:
        objective = (build_demand_weights(demand["indicators"], demand["population"], value_weights)
                     if value_weights else demand["population"] / max(demand["population"].sum(), 1))
        ctx.progress("Requested MCLP", 0, 1)
        solution = solve_mclp(travel["matrix"], objective, demand["population"], demand["vulnerable_mask"],
                              n_facilities, threshold_minutes, candidate_ids, "requested", value_weights,
                              _cfg(ctx, "optimization").get("solver_time_limit_s", 120),
                              coverage_target=coverage_target, coverage_mask=target_mask)
        ctx.progress("Requested MCLP", 1, 1)
    reachable = travel["matrix"] <= threshold_minutes
    max_tract_coverage = float(reachable[target_mask].any(axis=1).mean()) if target_mask.any() else 0.0
    nearest = np.min(travel["matrix"], axis=1) if len(reachable) else np.array([])
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
    covered = np.any(travel["matrix"][:, columns] <= threshold_minutes, axis=1) if columns else np.zeros(len(demand["population"]), bool)
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
    return ToolResult(ok=True, tool="optimize_sites", handle="analysis",
                      summary={"selected_ids": solution.selected_ids, "covered_ratio": solution.covered_ratio,
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
    candidate_ids = candidates["cand_id"].astype(str).tolist(); mask = _coverage_mask(demand, coverage_group)
    ctx.progress("LSCP", 0, 1)
    solution = solve_lscp(travel["matrix"], demand["population"], demand["vulnerable_mask"],
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
        coverage_map(demand["areas"], candidates, solution, "heat_exposure", summary["map"], study.get("boundary"))
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
    candidate_ids = candidates["cand_id"].astype(str).tolist(); mask = _coverage_mask(demand, coverage_group)
    threshold, solution = minimum_coverage_threshold(
        travel["matrix"], demand["population"], demand["vulnerable_mask"], candidate_ids,
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
        coverage_map(demand["areas"], candidates, solution, "heat_exposure", summary["map"], study.get("boundary"))
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
    demand, travel = ctx.get("demand"), ctx.get("travel"); matrix = np.asarray(travel["matrix"], dtype=float)
    mask = _coverage_mask(demand, coverage_group); nearest = matrix.min(axis=1)
    reachable_count = (matrix <= threshold_minutes).sum(axis=1)
    ids = (demand["areas"]["GEOID"].astype(str).tolist() if "GEOID" in demand["areas"]
           else [str(index) for index in range(len(matrix))])
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
    coverage_map(demand["areas"], candidates, analysis["solution"], indicator, paths["map"], study["boundary"])
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
