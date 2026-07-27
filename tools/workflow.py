"""Six coarse tools shared by the deterministic pipeline and the agent."""
from __future__ import annotations

import logging
from functools import wraps
from pathlib import Path
from typing import Callable

import geopandas as gpd
import numpy as np
import pandas as pd

from agent.memory import Context
from agent.schemas import Provenance, TaskSpec, ToolError, ToolResult, ValueWeights
from hitl.knobs import get_weights
from output.charts import pareto_chart
from output.maps import coverage_map
from output.report import render_report
from robustness.cache import scoped_cache
from tools.data_acquisition.census import derive_indicators, fetch_acs, fetch_county_geometry, fetch_tract_geometry
from tools.data_acquisition.geocoding import AmbiguousPlaceError, resolve_fips, resolve_place
from tools.data_acquisition.heat import fetch_heat_exposure
from tools.data_acquisition.osm import fetch_candidates
from tools.optimization.models import build_demand_weights, solve_mclp
from tools.optimization.pareto import coverage_pareto_sweep, non_dominated
from tools.registry import registry
from tools.spatial.accessibility import (haversine_matrix, sparse_network_matrix,
                                         travel_time_matrix, validate_travel_matrix)
from tools.spatial.alignment import centroids, pick_metric_crs

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
    areas["heat_exposure"] = areas["mean_lst"].fillna(areas["mean_lst"].median())
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
                      summary={"tracts": len(areas), "vulnerable_tracts": int(demand["vulnerable_mask"].sum())})


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
                    timeout_s=cfg.get("ors_timeout_s", 60))
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


@registry.register("optimize_sites", "Solve one requested siting profile and an efficiency-equity Pareto sweep.",
                   {"n_facilities": {"type": "integer"}, "threshold_minutes": {"type": "number"},
                    "weights": {"type": ["object", "null"]}})
@_guard("optimize_sites")
def optimize_sites(ctx: Context, n_facilities: int = 8, threshold_minutes: float = 15,
                   weights: dict | ValueWeights | None = None) -> ToolResult:
    """Solve the requested profile and the coverage Pareto sweep."""
    demand, candidates, travel = ctx.get("demand"), ctx.get("candidates"), ctx.get("travel")
    candidate_ids = candidates["cand_id"].astype(str).tolist()
    points = coverage_pareto_sweep(travel["matrix"], demand["population"], demand["vulnerable_mask"],
                                   candidate_ids, n_facilities, threshold_minutes)
    if weights is None: solution = points[0].solution
    else:
        value_weights = weights if isinstance(weights, ValueWeights) else ValueWeights(**weights)
        solution = solve_mclp(travel["matrix"], build_demand_weights(demand["indicators"], demand["population"], value_weights),
                              demand["population"], demand["vulnerable_mask"], n_facilities, threshold_minutes,
                              candidate_ids, "requested", value_weights,
                              _cfg(ctx, "optimization").get("solver_time_limit_s", 120))
    analysis = {"solution": solution, "points": points, "frontier": non_dominated(points),
                "n_facilities": n_facilities, "threshold_minutes": threshold_minutes}
    ctx.put("analysis", analysis)
    return ToolResult(ok=True, tool="optimize_sites", handle="analysis",
                      summary={"selected_ids": solution.selected_ids, "covered_ratio": solution.covered_ratio,
                               "vulnerable_covered_ratio": solution.vulnerable_covered_ratio,
                               "frontier_points": len(analysis["frontier"])})


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
                            "weights": task.weights.model_dump() if task.weights else None}),
        ("generate_outputs", {"output_dir": output_dir}),
    ]
    results = []
    for name, args in calls:
        result = registry.get(name).fn(ctx=ctx, **args); results.append(result)
        if not result.ok: return {"ok": False, "failed_tool": name, "results": results, "context": ctx}
    return {"ok": True, "outputs": ctx.get("outputs"), "solution": ctx.get("analysis")["solution"],
            "results": results, "context": ctx}
