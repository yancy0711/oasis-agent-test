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
from oasis.core.outputs.maps import (comparison_map, coverage_map, heat_exposure_map,
                                     reachability_map, tract_indicator_map)
from oasis.core.outputs.report import render_heat_report, render_report
from oasis.core.cache import scoped_cache
from oasis.core.cancellation import RunCancelled
from oasis.core.data.census import (derive_indicators, fetch_acs, fetch_block_group_population,
                                    fetch_county_geometry, fetch_tract_geometry)
from oasis.core.data.geocoding import AmbiguousPlaceError, resolve_fips, resolve_place
from oasis.core.data.heat import fetch_heat_exposure
from oasis.core.data.environment import derive_environment_burden, fetch_environment_indices
from oasis.core.data.population import population_raster_source
from oasis.core.data.osm import fetch_candidates
from oasis.core.data.overpass import OverpassUnavailable
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
from oasis.core.vulnerability import (DEFAULT_CONFIG as DEFAULT_VULNERABILITY_CONFIG,
                                      analyze_feature_relationships, analyze_vulnerability)

TOOL_CHAIN = ["resolve_study_area", "prepare_demand", "prepare_built_environment", "fetch_candidate_sites",
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
            except OverpassUnavailable as exc:
                return ToolResult(ok=False, tool=name, summary={"provider_unavailable": True},
                                  error=ToolError(kind="unknown", message=str(exc), retryable=False,
                                                  hint="The OSM candidate source is unchanged. Retry later; no CSV upload is required."))
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
    population = np.asarray(travel.get("population", demand["population"]), dtype=float)
    legacy_mask = np.asarray(
        travel.get("vulnerable_mask", demand.get("vulnerable_mask", np.zeros(len(population), dtype=bool))),
        dtype=bool,
    )
    return (
        np.asarray(travel["matrix"], dtype=float),
        population,
        np.asarray(travel.get("equity_population", demand.get("equity_population", population * legacy_mask)), dtype=float),
        travel.get("indicators", demand.get("indicators", {})),
    )


def _coverage_mask(demand: dict, group: str, travel: dict | None = None) -> np.ndarray:
    """Return the deterministic demand subset named by a public tool argument."""
    population = travel.get("population", demand["population"]) if travel else demand["population"]
    fallback = demand.get("priority_mask", demand.get("vulnerable_mask", np.zeros(len(population), dtype=bool)))
    priority = travel.get("priority_mask", travel.get("vulnerable_mask", fallback)) if travel else fallback
    if group == "all": return np.ones(len(population), dtype=bool)
    if group == "vulnerable": return np.asarray(priority, dtype=bool)
    raise ValueError(f"Unsupported demand group: {group}")


def _filter_candidate_hours(ctx: Context, candidates: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, dict]:
    """Apply one confirmed operating window and retain its quality summary."""
    task = ctx.get("task", None)
    filtered, summary = filter_candidates_by_hours(
        candidates, getattr(task, "operating_window", None)
    )
    if filtered.empty:
        raise ValueError("No candidate facilities remain under the confirmed operating-hours policy")
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


def _candidate_capacities(
    candidates: gpd.GeoDataFrame,
    default_capacity: float | None,
) -> tuple[np.ndarray | None, dict]:
    """Resolve hard people capacities without treating size proxies as people."""
    source_column = (
        "capacity_observed_people" if "capacity_observed_people" in candidates
        else "capacity" if "capacity" in candidates else None
    )
    observed = (
        pd.to_numeric(candidates[source_column], errors="coerce").to_numpy(float)
        if source_column else np.full(len(candidates), np.nan)
    )
    valid = np.isfinite(observed) & (observed > 0)
    evidence = {
        "observed_capacity_sites": int(valid.sum()),
        "missing_capacity_sites": int((~valid).sum()),
        "source_column": source_column,
        "relative_floor_area_proxy_sites": int(candidates.get(
            "gross_floor_area_proxy_m2", pd.Series(dtype=float)
        ).notna().sum()),
    }
    if default_capacity is not None:
        capacities = np.where(valid, observed, float(default_capacity))
        evidence.update({"enabled": True, "method": "observed_with_user_default"})
        return capacities, evidence
    if len(candidates) and valid.all():
        evidence.update({"enabled": True, "method": "observed_people_capacity"})
        return observed, evidence
    evidence.update({
        "enabled": False,
        "method": "disabled_incomplete_absolute_capacity",
        "reason": (
            "Hard capacity was not enabled because absolute people capacity is incomplete. "
            "Floor area remains a relative diagnostic only."
        ),
    })
    return None, evidence


def _filter_candidate_access(
    candidates: gpd.GeoDataFrame,
) -> tuple[gpd.GeoDataFrame, dict]:
    """Exclude explicitly restricted or paid facilities and retain auditable unknowns."""
    access = (
        candidates["access"].fillna("").astype(str).str.strip().str.lower()
        if "access" in candidates
        else pd.Series("", index=candidates.index, dtype=str)
    )
    fee = (candidates["fee"].fillna("").astype(str).str.strip().str.lower()
           if "fee" in candidates else pd.Series("", index=candidates.index, dtype=str))
    charge = (candidates["charge"].fillna("").astype(str).str.strip().str.lower()
              if "charge" in candidates else pd.Series("", index=candidates.index, dtype=str))
    access_restricted = access.isin({"no", "private", "customers", "members", "permit"})
    fee_required = fee.isin({"yes", "admission", "ticket"}) | (~charge.isin({"", "0", "no", "free"}))
    excluded = access_restricted | fee_required
    result = candidates.loc[~excluded].copy().reset_index(drop=True)
    retained_access = access.loc[~excluded].reset_index(drop=True)
    retained_fee = fee.loc[~excluded].reset_index(drop=True)
    result["public_access_status"] = np.select(
        [retained_access.isin({"yes", "permissive"}), retained_access.eq("")],
        ["osm_public", "unknown"],
        default="conditional",
    )
    result["fee_status"] = np.where(retained_fee.eq("no"), "free", "unknown")
    if "cooling_service_status" not in result:
        result["cooling_service_status"] = "unverified"
    summary = {
        "potential_candidates": len(candidates),
        "retained_candidates": len(result),
        "restricted_candidates": int(access_restricted.sum()),
        "fee_required_candidates": int((fee_required & ~access_restricted).sum()),
        "unknown_access_candidates": int(retained_access.eq("").sum()),
        "unknown_fee_candidates": int(retained_fee.eq("").sum()),
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
            if kind == "block_groups":
                return fetch_block_group_population(state, county, year)
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
def resolve_study_area(ctx: Context, place_name: str, year: int | None = None) -> ToolResult:
    """Resolve a place boundary and its state/county FIPS identifiers."""
    year = int(year or _cfg(ctx, "data").get("census_vintage", 2022))
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

    population_source = (
        population_raster_source(year, data_cfg["population_grid"])
        if "population_grid" in data_cfg else None
    )
    kinds = ["acs", "tracts"] + (["block_groups"] if population_source and population_source["provider"] == "census_block_group" else [])
    parts = {kind: [None] * len(counties) for kind in kinds}
    total_tasks = len(kinds) * len(counties)
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
            for kind in kinds
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
    if any(part is None for kind in kinds for part in parts[kind]):
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
    population_areas = None
    if "block_groups" in parts:
        population_areas = gpd.GeoDataFrame(
            pd.concat(parts["block_groups"], ignore_index=True), geometry="geometry",
            crs=parts["block_groups"][0].crs,
        )
    heat_error = None
    try:
        with scoped_cache(study["cache_scope"]):
            heat, heat_prov = fetch_heat_exposure(
                areas[["GEOID", "geometry"]],
                start_date,
                end_date,
                readable_workers=data_cfg.get("landsat_probe_workers", 8),
                dask_workers=data_cfg.get("dask_workers", 4),
                progress_callback=lambda label, done, total: ctx.progress(
                    label, done, total, unit="stages"
                ),
                cancel_check=ctx.check_cancelled,
                population_source=population_source,
                tract_population=areas["total_population"].to_numpy(float),
                population_areas=population_areas,
            )
    except RunCancelled:
        raise
    except Exception as exc:
        if getattr(ctx.get("task", None), "analysis_goal", None) == "heat_review":
            raise RuntimeError(f"Landsat heat exposure acquisition failed: {exc}") from exc
        heat_error = f"{type(exc).__name__}: {exc}"
        log.warning("Landsat heat unavailable; continuing with a neutral non-temperature indicator: %s", exc)
        heat = pd.DataFrame({
            "GEOID": areas["GEOID"].astype("string"), "mean_lst": np.nan,
            "population_weighted_lst": np.nan, "exposure_gap": np.nan,
            "valid_lst_population_share": np.nan,
        })
        heat_prov = Provenance(
            source="Landsat 8/9 Collection 2 Level-2 via Microsoft Planetary Computer",
            tier="live", record_count=0, crs="none",
            notes=f"unavailable; neutral heat indicator used for non-heat analysis; {heat_error}",
        )
    ctx.check_cancelled()
    areas = areas.merge(heat, on="GEOID", how="left", validate="one_to_one")
    minimum_share = float((population_source or {}).get("minimum_valid_population_share", .8))
    weighted = areas.get("population_weighted_lst", pd.Series(np.nan, index=areas.index))
    valid_share = areas.get("valid_lst_population_share", pd.Series(np.nan, index=areas.index))
    weighted_ok = weighted.notna() & valid_share.ge(minimum_share)
    areas["heat_exposure"] = weighted.where(weighted_ok, areas["mean_lst"])
    weighted_method = (
        "population_weighted_block_group"
        if population_source and population_source.get("provider") == "census_block_group"
        else "population_weighted_grid"
    )
    areas["heat_exposure_method"] = (
        "unavailable_neutral" if heat_error else
        np.where(weighted_ok, weighted_method, "tract_mean_fallback")
    )
    missing_heat = int(areas["heat_exposure"].isna().sum())
    heat_median = areas["heat_exposure"].median()
    areas["heat_exposure"] = areas["heat_exposure"].fillna(
        float(heat_median) if np.isfinite(heat_median) else 0.0
    )
    quality = ctx.get("quality", {})
    quality["heat"] = {"missing_tracts": missing_heat, "total_tracts": len(areas),
                       "population_weighted_tracts": int(weighted_ok.sum()),
                       "tract_mean_fallbacks": int((~weighted_ok).sum()),
                       "minimum_valid_population_share": minimum_share,
                       "population_grid": population_source,
                       "status": "unavailable" if heat_error else "ready",
                       "error": heat_error,
                       "imputation": ("neutral constant; not interpretable as temperature" if heat_error
                                      else "median" if missing_heat else "none")}
    ctx.put("quality", quality)
    metric_crs = pick_metric_crs(areas)
    areas["population_density"] = areas["total_population"].astype(float) / _land_area_km2(areas).replace(0, np.nan)
    areas = areas.loc[areas["total_population"].fillna(0).gt(0)].reset_index(drop=True)
    points, population = centroids(areas).reset_index(drop=True), areas["total_population"].to_numpy(float)
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
    vulnerability_cfg = _cfg(ctx, "vulnerability") or DEFAULT_VULNERABILITY_CONFIG
    custom_vulnerability_weights = None
    task_weights = getattr(ctx.get("task", None), "weights", None)
    if task_weights:
        public_weights = task_weights.normalized().model_dump()
        custom_vulnerability_weights = {
            name: public_weights.get(name, 0.0)
            for name in vulnerability_cfg.get("indicators", {})
        }
    areas, vulnerability_score, priority_mask, vulnerability = analyze_vulnerability(
        areas, vulnerability_cfg, custom_vulnerability_weights,
        progress=lambda label, done, total: ctx.progress(label, done, total, unit="stages"),
    )
    quality["vulnerability"] = vulnerability
    ctx.put("quality", quality)
    vulnerability_missing = ",".join(
        f"{item['indicator']}:{item['missing_count']}/{len(areas)}"
        for item in vulnerability["missingness"]
    )
    ctx.put("vulnerability_analysis", vulnerability, Provenance(
        source="Configured vulnerability preprocessing", tier="live",
        record_count=len(areas), crs=str(areas.crs or "none"),
        notes=(f"normalization={vulnerability['normalization']}; "
               f"active_scheme={vulnerability['active_scheme']}; "
               f"priority={vulnerability['priority_definition']}; "
               f"missing={vulnerability_missing}; "
               "directions and missing-value policies are explicit in config/settings.yaml"),
    ))
    indicators = {name: areas[name].to_numpy(float) for name in
                  ["population_density", "elderly_ratio", "poverty_rate", "heat_exposure"]}
    indicators["vulnerability_score"] = vulnerability_score
    elderly_population = areas.get(
        "elderly_population", areas["elderly_ratio"].fillna(0) * areas["total_population"]
    )
    below_poverty = areas.get(
        "below_poverty", areas["poverty_rate"].fillna(0) * areas["total_population"]
    )
    no_vehicle_households = areas.get(
        "no_vehicle_households", areas["no_vehicle_ratio"].fillna(0) * areas["total_population"]
    )
    group_population = {
        "all": population,
        "older_adults": elderly_population.fillna(0).to_numpy(float),
        "below_poverty": below_poverty.fillna(0).to_numpy(float),
        "no_vehicle_households": no_vehicle_households.fillna(0).to_numpy(float),
        "vulnerability_priority": population * priority_mask,
    }
    demand = {"areas": areas, "points": points, "population": population,
              "no_vehicle_ratio": areas["no_vehicle_ratio"].to_numpy(float),
              "group_population": group_population,
              "equity_population": group_population["vulnerability_priority"],
              "priority_mask": priority_mask,
              "vulnerable_mask": priority_mask, "indicators": indicators}
    provenance = [Provenance(source=f"US Census ACS {year} 5-year", record_count=len(demographics), crs="none"),
                  Provenance(source=f"US Census TIGER/Line {year}", record_count=len(tracts))]
    if population_source:
        provenance.append(Provenance(
            source=(f"US Census ACS {year} 5-year block-group population + TIGER/Line {year}"
                    if population_source["provider"] == "census_block_group" else
                    f"WorldPop Global2 {year} constrained population grid"),
            tier="cache" if heat_prov.tier == "cache" else "live",
            record_count=(len(population_areas) if population_areas is not None else int(weighted_ok.sum())),
            crs="EPSG:4326",
            notes=(
                f"resolution={population_source.get('resolution')}; "
                f"ACS tract total calibration; weighted_tracts={int(weighted_ok.sum())}; "
                f"fallback_tracts={int((~weighted_ok).sum())}; source={population_source.get('url')}"
            ),
        ))
    provenance.append(heat_prov)
    ctx.put("demand", demand, provenance)
    return ToolResult(ok=True, tool="prepare_demand", handle="demand", provenance=heat_prov,
                      summary={"tracts": len(areas), "vulnerability_priority_tracts": int(priority_mask.sum()),
                               "vulnerability_definition": vulnerability["priority_definition"],
                               "equity_group": "population_in_composite_vulnerability_priority_tracts",
                               "population_weighted_heat_tracts": int(weighted_ok.sum()),
                               "heat_missing_tracts": missing_heat, "heat_imputation": quality["heat"]["imputation"],
                               "vehicle_missing_tracts": vehicle_missing,
                               "mean_no_vehicle_household_ratio": float(areas["no_vehicle_ratio"].mean())})


@registry.register(
    "prepare_built_environment",
    "Derive Landsat tract indices and a user-weightable passive environment burden.",
)
@_guard("prepare_built_environment")
def prepare_built_environment(
    ctx: Context,
    demand_handle: str = "demand",
    start_date: str = "2022-06-01",
    end_date: str = "2022-08-31",
) -> ToolResult:
    """Attach built-environment diagnostics and a transparent optimization indicator."""
    demand = ctx.get(demand_handle)
    cfg = _cfg(ctx, "data")
    settings = cfg.get("landsat_environment_indices", {})
    if not settings.get("enabled", True):
        summary = {"status": "skipped", "reason": "disabled for this execution phase"}
        ctx.put("built_environment", summary)
        return ToolResult(ok=True, tool="prepare_built_environment", handle="built_environment", summary=summary)
    study = ctx.get("study")
    try:
        with scoped_cache(study["cache_scope"]):
            indices, provenance = fetch_environment_indices(
                demand["areas"][["GEOID", "geometry"]], start_date, end_date,
                resolution=settings.get("resolution_m", 120),
                dask_workers=cfg.get("dask_workers", 4),
                cancel_check=ctx.check_cancelled,
                progress_callback=lambda label, done, total: ctx.progress(
                    label, done, total, unit="stages"
                ),
            )
    except RunCancelled:
        raise
    except Exception as exc:
        areas = demand["areas"].copy()
        for name in ("mean_ndvi", "mean_ndbi", "mean_albedo"):
            areas[name] = np.nan
        areas["environment_components_available"] = 0
        areas["environment_burden"] = 0.5
        indicators = dict(demand["indicators"])
        indicators["environment_burden"] = np.full(len(areas), 0.5)
        demand = {**demand, "areas": areas, "indicators": indicators}
        summary = {
            "status": "unavailable",
            "reason": f"{type(exc).__name__}: {exc}",
            "role": "neutral_constant_not_interpretable_as_environmental_difference",
            "path_heat_exposure": "not_in_objective_without_real_route_geometry_for_each_demand_site_pair",
        }
        provenance = Provenance(
            source="Landsat 8/9 Collection 2 Level-2 surface reflectance",
            tier="live", record_count=0, crs="none",
            notes=f"NDVI, NDBI, and albedo unavailable; neutral burden used; {summary['reason']}",
        )
        ctx.put("demand", demand, provenance)
        ctx.put("built_environment", summary, provenance)
        quality = ctx.get("quality", {}); quality["built_environment"] = summary
        ctx.put("quality", quality)
        return ToolResult(ok=True, tool="prepare_built_environment",
                          handle="built_environment", provenance=provenance, summary=summary)
    areas = demand["areas"].drop(
        columns=[name for name in ("mean_ndvi", "mean_ndbi", "mean_albedo", "environment_valid_pixel_count")
                 if name in demand["areas"]]
    ).merge(indices, on="GEOID", how="left", validate="one_to_one")
    burden = derive_environment_burden(areas)
    for name in burden:
        areas[name] = burden[name]
    indicators = dict(demand["indicators"])
    indicators["environment_burden"] = areas["environment_burden"].to_numpy(float)
    demand = {**demand, "areas": areas, "indicators": indicators}
    fields = ["mean_ndvi", "mean_ndbi", "mean_albedo"]
    relationships = analyze_feature_relationships(areas, {
        "elderly_ratio": "vulnerability input",
        "poverty_rate": "vulnerability input",
        "heat_exposure": "vulnerability input and temperature exposure",
        "vulnerability_score": "derived composite score",
        "mean_ndvi": "passive environment component: higher is protective",
        "mean_ndbi": "passive environment component: higher increases burden",
        "mean_albedo": "passive environment component: higher is protective",
        "environment_burden": "user-weightable passive environment objective indicator",
    })
    summary = {
        "status": "ready", "role": "optimization_indicator_when_confirmed_weight_is_positive",
        "available_tracts": {name: int(areas[name].notna().sum()) for name in fields},
        "formulas": {
            "mean_ndvi": "(NIR - red) / (NIR + red)",
            "mean_ndbi": "(SWIR1 - NIR) / (SWIR1 + NIR)",
            "mean_albedo": "broadband albedo proxy from blue, red, NIR, SWIR1, and SWIR2",
            "environment_burden": "mean(percentile(NDBI), 1-percentile(NDVI), 1-percentile(albedo))",
        },
        "environment_burden": {
            "minimum": float(areas["environment_burden"].min()),
            "maximum": float(areas["environment_burden"].max()),
            "median": float(areas["environment_burden"].median()),
            "all_components_tracts": int((areas["environment_components_available"] == 3).sum()),
            "direction": "higher_is_more_burdened",
        },
        "path_heat_exposure": "not_in_objective_without_real_route_geometry_for_each_demand_site_pair",
        "feature_relationships": relationships,
    }
    vulnerability = dict(ctx.get("vulnerability_analysis", {}))
    vulnerability["diagnostic_correlation"] = relationships
    ctx.put("vulnerability_analysis", vulnerability)
    quality = ctx.get("quality", {})
    quality["built_environment"] = summary
    ctx.put("quality", quality)
    ctx.put("demand", demand, provenance)
    ctx.put("built_environment", summary)
    return ToolResult(ok=True, tool="prepare_built_environment", handle="built_environment",
                      provenance=provenance, summary=summary)


@registry.register("fetch_candidate_sites", "Fetch OSM candidate facilities inside the study boundary.",
                   {"amenities": {"type": "array", "items": {"type": "string"}}})
@_guard("fetch_candidate_sites")
def fetch_candidate_sites(ctx: Context, amenities: list[str], study_handle: str = "study") -> ToolResult:
    """Fetch candidate facility points for the requested OSM amenities."""
    study = ctx.get(study_handle)
    ctx.progress("Candidate facilities: querying OpenStreetMap", 0, 3, unit="stages")
    with scoped_cache(study["cache_scope"]):
        candidates, error = fetch_candidates(study["boundary"], amenities, check=ctx.check_cancelled, progress=ctx.progress)
    if error: return ToolResult(ok=False, tool="fetch_candidate_sites", error=error)
    acquisition = candidates.attrs.get("osm_acquisition", {})
    ctx.progress("Candidate facilities: received candidate records", 1, 3, unit="stages")
    ctx.progress("Candidate facilities: checking access and opening hours", 2, 3, unit="stages")
    potential = len(candidates)
    candidates, access = _filter_candidate_access(candidates)
    candidates, hours = _filter_candidate_hours(ctx, candidates)
    capacity_summary = {
        "observed_capacity_sites": int(candidates.get(
            "capacity_observed_people", pd.Series(dtype=float)
        ).notna().sum()),
        "relative_floor_area_proxy_sites": int(candidates.get(
            "gross_floor_area_proxy_m2", pd.Series(dtype=float)
        ).notna().sum()),
        "policy": (
            "Only observed OSM/CSV people capacities become hard constraints. "
            "Floor area is reported as relative evidence and is not auto-converted to people."
        ),
    }
    quality = ctx.get("quality", {})
    quality["public_access"] = access
    ctx.put("quality", quality)
    prov = Provenance(
        source="OpenStreetMap candidate facilities, access, opening hours, and fee tags",
        tier="cache" if acquisition.get("cache_parts") and not acquisition.get("live_parts") else "live",
        record_count=len(candidates),
        notes=(f"potential facilities: {potential}; retained: {len(candidates)}; "
               f"restricted access: {access['restricted_candidates']}; "
               f"fee required: {access['fee_required_candidates']}; "
               f"hours unknown: {hours.get('unknown_candidates', 0)}; "
               f"observed capacity: {capacity_summary['observed_capacity_sites']}; "
               f"floor area proxy: {capacity_summary['relative_floor_area_proxy_sites']}; "
               f"OSM acquisition: {acquisition}"),
    )
    ctx.put("candidates", candidates, prov)
    ctx.progress("Candidate facilities: ready", 3, 3, unit="stages")
    return ToolResult(ok=True, tool="fetch_candidate_sites", handle="candidates", provenance=prov,
                      summary={"candidates": len(candidates), "amenities": amenities,
                               "public_access": access, "operating_hours": hours,
                               "capacity_evidence": capacity_summary, "osm_acquisition": acquisition})


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
        notes=(f"potential facilities: {potential}; retained: {len(candidates)}; "
               f"restricted access: {access['restricted_candidates']}; "
               f"fee required: {access['fee_required_candidates']}; "
               f"hours unknown: {hours.get('unknown_candidates', 0)}"),
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
    map_path, chart_path, report_path = (str(out / "heat_exposure_map.geojson"),
                                         str(out / "heat_analysis.png"), str(out / "heat_report.md"))
    heat_exposure_map(areas, map_path, study["boundary"])
    statistics = heat_analysis_chart(areas, chart_path)
    methods = areas.get("heat_exposure_method", pd.Series(dtype=str)).value_counts().to_dict()
    summary = {"top_tracts": hottest.to_dict("records"), "tracts_reviewed": len(areas),
               "statistics": statistics, "map": map_path, "analysis_chart": chart_path, "report": report_path}
    summary["evidence_basis"] = (
        "Landsat 8/9 Collection 2 Level-2 land-surface temperature, population-weighted where "
        f"the configured population evidence was valid; recorded aggregation methods: {methods or 'not reported'}."
    )
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

    upper_speeds = cfg.get("ors_upper_speeds_kmh", {})
    pruning_margin = cfg.get("ors_pruning_margin", 1.5)
    eligible_masks = {}
    for mode in modes:
        lower_bound = haversine_matrix(
            demand["points"],
            candidates,
            upper_speeds.get(mode, cfg.get("ors_upper_speed_kmh", 6.0)),
            1.0,
        )
        eligible_masks[mode] = lower_bound <= policy_minutes * pruning_margin

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
                "eligible_pairs": int(eligible_masks[mode].sum()),
                "pruned_pairs": int((~eligible_masks[mode]).sum()),
            }
    else:
        with scoped_cache(cache_scope):
            for mode in modes:
                eligible = eligible_masks[mode]
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
                    max_block_route_expansion=cfg.get("ors_max_block_route_expansion", 1.5),
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
        "policy_minutes": policy_minutes,
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
                    "coverage_group": {"type": "string", "enum": ["all", "vulnerable"]},
                    "facility_capacity": {"type": ["number", "null"]},
                    "equity_alpha": {"type": ["number", "null"]}})
@_guard("optimize_sites")
def optimize_sites(ctx: Context, n_facilities: int = 10, threshold_minutes: float = 15,
                   weights: dict | ValueWeights | None = None, coverage_target: float | None = None,
                   coverage_group: str = "all",
                   facility_capacity: float | None = None,
                   equity_alpha: float | None = None) -> ToolResult:
    """Solve gradual coverage with optional capacity and explicit assignment."""
    demand, candidates, travel = ctx.get("demand"), ctx.get("candidates"), ctx.get("travel")
    matrix, population, vulnerable, indicators = _decision_inputs(demand, travel)
    candidate_ids = candidates["cand_id"].astype(str).tolist(); candidate_count = len(candidate_ids)
    if facility_capacity is not None and facility_capacity <= 0:
        raise ValueError("facility_capacity must be positive")
    if equity_alpha is not None and not 0 <= equity_alpha <= 1:
        raise ValueError("equity_alpha must be between zero and one")
    capacities, capacity_evidence = _candidate_capacities(candidates, facility_capacity)
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
        None if capacities is None else tuple(np.round(capacities, 6)),
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

    pareto_progress("Pareto efficiency baseline", 0, 1)
    points = coverage_pareto_sweep(
        matrix,
        population,
        vulnerable,
        candidate_ids,
        n_facilities,
        threshold_minutes,
        progress_callback=pareto_progress,
        max_workers=optimization_cfg.get("pareto_workers", 4),
        time_limit_s=optimization_cfg.get("pareto_point_time_limit_s", 30),
        total_time_limit_s=optimization_cfg.get("pareto_total_time_limit_s", 90),
        cancel_check=ctx.check_cancelled,
        checkpoint_callback=checkpoint,
        completed_points=completed_points,
        facility_capacities=capacities,
    )
    value_weights = weights if isinstance(weights, ValueWeights) else (ValueWeights(**weights) if weights else None)
    selected_alpha = None
    if equity_alpha is not None:
        selected_point = min(points, key=lambda point: (abs(point.alpha - equity_alpha), point.alpha))
        solution = selected_point.solution
        selected_alpha = selected_point.alpha
    elif value_weights is None and coverage_target is None:
        solution = points[0].solution
    else:
        objective = (build_demand_weights(indicators, population, value_weights)
                     if value_weights else population / max(population.sum(), 1))
        ctx.progress("Requested gradual coverage", 0, 1)
        solution = solve_mclp(matrix, objective, population, vulnerable,
                              n_facilities, threshold_minutes, candidate_ids, "requested", value_weights,
                              optimization_cfg.get("solver_time_limit_s", 120),
                              coverage_target=coverage_target, coverage_mask=target_mask,
                              facility_capacities=capacities, cancel_check=ctx.check_cancelled,
                              heartbeat=lambda: ctx.progress("Requested gradual coverage", 0, 1,
                                  solve_time_limit_seconds=solver_time_limit))
        ctx.progress("Requested gradual coverage", 1, 1)
    reachable = matrix <= threshold_minutes
    max_tract_coverage = float(reachable[target_mask].any(axis=1).mean()) if target_mask.any() else 0.0
    nearest = np.min(matrix, axis=1) if len(reachable) else np.array([])
    target_nearest = nearest[target_mask] if len(nearest) else np.array([])
    minimum_full_threshold = float(target_nearest.max()) if len(target_nearest) and np.isfinite(target_nearest).all() else None
    diagnostics = {"requested_facilities": n_facilities, "candidate_count": candidate_count,
                   "coverage_target": coverage_target, "coverage_group": coverage_group,
                   "coverage_model": "linear_gradual_assignment",
                   "capacity_enabled": capacities is not None,
                   "capacity_evidence": capacity_evidence,
                   "facility_capacity_default": facility_capacity,
                   "requested_equity_alpha": equity_alpha,
                   "selected_frontier_alpha": selected_alpha,
                   "pareto_completed_points": len(points),
                   "pareto_requested_points": 11,
                   "pareto_complete": len(points) == 11,
                   "final_solution_basis": ("efficiency_gini_frontier" if selected_alpha is not None
                                            else "confirmed_value_profile" if value_weights else "population_efficiency"),
                   "max_tract_coverage_at_threshold": max_tract_coverage,
                   "minimum_threshold_with_all_candidates": minimum_full_threshold}
    if solution.solver_status not in {"Optimal", "Feasible"}:
        ctx.put("constraint_diagnostics", diagnostics)
        timed_out = solution.solver_status != "Infeasible"
        if timed_out:
            hint = "Retry the same inputs or allow more solver time. Feasibility has not been established."
        elif coverage_target is not None and max_tract_coverage + 1e-9 < coverage_target:
            hint = "Even all current candidates cannot meet the target at this threshold; increase the threshold or expand candidates."
        else: hint = "Increase the facility budget or threshold, or relax the coverage target."
        analysis = {"solution": solution, "points": points, "frontier": non_dominated(points),
                    "n_facilities": n_facilities, "threshold_minutes": threshold_minutes,
                    "coverage_target": coverage_target, "diagnostics": diagnostics}
        ctx.put("analysis", analysis)
        return ToolResult(ok=False, tool="optimize_sites", summary=diagnostics,
                          error=ToolError(kind="timeout" if timed_out else "constraint_conflict",
                                          message=("The requested coverage solve reached its time limit without a verified solution."
                                                   if timed_out else "The confirmed constraints are infeasible under the current candidates and travel matrix."),
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
                "population_weighted_access_gini": solution.population_weighted_access_gini,
                "mean_access_score": solution.mean_access_score,
                "gradual_coverage_ratio": solution.gradual_coverage_ratio,
                "assigned_population": solution.assigned_population,
                "maximum_capacity_utilization": solution.maximum_capacity_utilization,
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
                               "population_weighted_access_gini": solution.population_weighted_access_gini,
                               "mean_access_score": solution.mean_access_score,
                               "gradual_coverage_ratio": solution.gradual_coverage_ratio,
                               "assigned_population": solution.assigned_population,
                               "facility_loads": solution.facility_loads,
                               "facility_capacities": solution.facility_capacities,
                               "maximum_capacity_utilization": solution.maximum_capacity_utilization,
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
                          _cfg(ctx, "optimization").get("solver_time_limit_s", 120),
                          cancel_check=ctx.check_cancelled,
                          heartbeat=lambda: ctx.progress("LSCP", 0, 1))
    ctx.progress("LSCP", 1, 1)
    feasible = solution.solver_status in {"Optimal", "Feasible"}
    current = ctx.get("task", None)
    minimum = len(solution.selected_ids) if solution.solver_status == "Optimal" else None
    summary = {"feasible": feasible, "minimum_facilities": minimum,
               "minimum_proven": solution.solver_status == "Optimal",
               "feasible_facility_count": len(solution.selected_ids) if feasible else None,
               "current_facilities": getattr(current, "n_facilities", None),
               "facility_gap": (minimum - current.n_facilities if minimum is not None and current else None),
               "threshold_minutes": threshold_minutes, "coverage_target": coverage_target,
               "coverage_group": coverage_group, "solver_status": solution.solver_status,
               "selected_ids": solution.selected_ids}
    study = ctx.get("study", {})
    if feasible and study and "areas" in demand:
        out = Path(_cfg(ctx, "output").get("dir", "outputs_run")) / study["cache_scope"]
        out.mkdir(parents=True, exist_ok=True); summary["map"] = str(out / "minimum_facilities_map.geojson")
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
def find_minimum_threshold(ctx: Context, n_facilities: int = 10,
                           coverage_target: float = 1.0, coverage_group: str = "all") -> ToolResult:
    """Search exact observed travel times for the minimum feasible threshold."""
    demand, candidates, travel = ctx.get("demand"), ctx.get("candidates"), ctx.get("travel")
    matrix, population, vulnerable, _ = _decision_inputs(demand, travel)
    candidate_ids = candidates["cand_id"].astype(str).tolist(); mask = _coverage_mask(demand, coverage_group, travel)
    optimization = _cfg(ctx, "optimization")
    search_diagnostics = {}
    threshold, solution = minimum_coverage_threshold(
        matrix, population, vulnerable, candidate_ids,
        n_facilities, coverage_target, mask,
        optimization.get("solver_time_limit_s", 120), ctx.progress,
        total_time_limit_s=optimization.get("minimum_threshold_total_time_limit_s", 90),
        search_diagnostics=search_diagnostics,
        cancel_check=ctx.check_cancelled,
    )
    current = ctx.get("task", None)
    summary = {"feasible": threshold is not None, "minimum_threshold_minutes": threshold,
               "current_threshold_minutes": getattr(current, "threshold_minutes", None),
               "threshold_gap_minutes": (threshold - current.threshold_minutes if threshold is not None and current else None),
               "n_facilities": n_facilities, "coverage_target": coverage_target,
               "coverage_group": coverage_group,
               "search": search_diagnostics,
               "search_complete": search_diagnostics.get("status") == "complete",
               "selected_ids": solution.selected_ids if solution else []}
    study = ctx.get("study", {})
    if solution and threshold is not None and study and "areas" in demand:
        out = Path(_cfg(ctx, "output").get("dir", "outputs_run")) / study["cache_scope"]
        out.mkdir(parents=True, exist_ok=True); summary["map"] = str(out / "minimum_threshold_map.geojson")
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
    areas_wgs84 = demand["areas"].to_crs("EPSG:4326")
    locations = {}
    for _, area in areas_wgs84.iterrows():
        geometry = area.geometry
        if geometry is None or geometry.is_empty:
            continue
        point = geometry.representative_point()
        locations[str(area.get("GEOID", ""))] = {
            "longitude": float(point.x), "latitude": float(point.y),
        }
    for rank, row in enumerate(rows, start=1):
        geoid = str(row["demand_id"]).split(":", 1)[0]
        row.update({"GEOID": geoid, "rank": rank, **locations.get(geoid, {})})
    study = ctx.get("study")
    out = Path(_cfg(ctx, "output").get("dir", "outputs_run"))
    out.mkdir(parents=True, exist_ok=True)
    map_path = str(out / "reachability_map.geojson")
    reachability_map(demand["areas"], rows, map_path, study.get("boundary"))
    summary = {"coverage_group": coverage_group, "threshold_minutes": threshold_minutes,
               "maximum_tract_coverage": float((reachable_count[mask] > 0).mean()),
               "unserved_demand_ids": [ids[index] for index in np.flatnonzero(mask & (reachable_count == 0))],
               "structurally_unreachable_ids": [ids[index] for index in np.flatnonzero(mask & ~np.isfinite(nearest))],
               "hardest_demand": rows[:max(1, limit)], "map": map_path,
               "evidence_basis": (
                   f"Nearest-candidate travel times use the {(travel.get('routing_report') or {}).get('router', travel.get('router', 'recorded'))} "
                   f"matrix and the confirmed {threshold_minutes:g}-minute threshold."
               )}
    ctx.put("reachability_analysis", summary)
    return ToolResult(ok=True, tool="analyze_reachability", handle="reachability_analysis", summary=summary)


def _answer_result_query(ctx: Context, request, query_index: int) -> dict:
    """Evaluate one typed, read-only result query over already computed handles."""
    task, demand = ctx.get("task"), ctx.get("demand")
    if request.metric == "unsupported":
        raise ValueError(
            "The requested tract property is not available in the approved run evidence; "
            "add a reviewed data source before ranking it."
        )
    areas = demand["areas"].copy().reset_index(drop=True)
    metric = request.metric
    labels = {
        "heat_exposure": "heat exposure",
        "vulnerability": "composite vulnerability",
        "access_disadvantage": "access disadvantage",
        "combined_disadvantage": "combined vulnerability and access disadvantage",
        "coverage_gap": "population share outside the confirmed access threshold",
    }
    heat_methods = areas.get("heat_exposure_method", pd.Series(dtype=str)).value_counts().to_dict()
    heat_basis = (
        "Landsat 8/9 Collection 2 Level-2 land-surface temperature, population-weighted where "
        f"the configured population evidence was valid; recorded aggregation methods: {heat_methods or 'not reported'}."
    )
    vulnerability = ctx.get("vulnerability_analysis", {})
    indicator_names = (vulnerability.get("correlation") or {}).get("indicators", [])
    active_scheme = vulnerability.get("active_scheme", "configured")
    active_weights = ((vulnerability.get("weight_sensitivity") or {}).get("schemes") or {}).get(active_scheme, {})
    indicator_labels = {
        "elderly_ratio": "elderly ratio", "poverty_rate": "poverty rate",
        "heat_exposure": "heat exposure", "environment_burden": "environment burden",
        "population_density": "population density",
    }
    weight_terms = [
        f"{float(weight):.2f} R({indicator_labels.get(str(name), str(name).replace('_', ' '))})"
        for name, weight in active_weights.items() if isinstance(weight, (int, float)) and float(weight) != 0
    ]
    vulnerability_formula = "V_i = " + " + ".join(weight_terms) if weight_terms else "V_i = sum_k beta_k R(x_ik)"
    vulnerability_basis = (
        f"The {active_scheme.replace('_', ' ')} vulnerability composite uses audited, "
        f"percentile-normalized {', '.join(indicator_labels.get(str(name), str(name).replace('_', ' ')) for name in indicator_names) or 'configured indicators'}."
    )
    map_indicator = metric
    if metric == "heat_exposure":
        values = pd.to_numeric(areas["heat_exposure"], errors="coerce")
    elif metric == "vulnerability":
        values = pd.to_numeric(areas["vulnerability_score"], errors="coerce")
        map_indicator = "vulnerability_score"
    else:
        travel, candidates = ctx.get("travel"), ctx.get("candidates")
        matrix, population, _, _ = _decision_inputs(demand, travel)
        candidate_ids = candidates["cand_id"].astype(str).tolist()
        selected_ids = (
            ctx.get("analysis")["solution"].selected_ids
            if ctx.has("analysis") else candidate_ids
        )
        selected = [index for index, candidate_id in enumerate(candidate_ids)
                    if candidate_id in set(map(str, selected_ids))]
        if not selected:
            raise ValueError("The confirmed analysis has no candidate facilities to rank access")
        router = (travel.get("routing_report") or {}).get("router", travel.get("router", "recorded travel matrix"))
        router_label = {
            "ors": "OpenRouteService network", "ors_sparse": "cached OpenRouteService network",
            "haversine": "Haversine approximation",
        }.get(str(router), str(router).replace("_", " "))
        candidate_basis = "selected facilities" if ctx.has("analysis") else "all retained candidate facilities"
        access_basis = (
            f"Travel times come from the {router_label} matrix to {candidate_basis}, using the confirmed "
            f"{float(task.threshold_minutes):g}-minute threshold."
        )
        nearest = np.min(matrix[:, selected], axis=1)
        disadvantage = np.where(
            np.isfinite(nearest), np.clip(nearest / float(task.threshold_minutes), 0.0, 1.0), 1.0
        )
        base_index = np.asarray(travel.get("base_index", np.arange(len(matrix))), dtype=int)
        grouped = pd.DataFrame({"base": base_index, "value": disadvantage, "population": population})
        grouped["weighted"] = grouped["value"] * grouped["population"]
        totals = grouped.groupby("base", sort=False)[["weighted", "population"]].sum()
        access = totals["weighted"] / totals["population"].replace(0, np.nan)
        areas["access_disadvantage"] = pd.Series(access, index=access.index).reindex(areas.index)
        if metric == "access_disadvantage":
            values = areas["access_disadvantage"]
        elif metric == "coverage_gap":
            gap = np.where(np.isfinite(nearest) & (nearest < float(task.threshold_minutes)), 0.0, 1.0)
            gap_frame = pd.DataFrame({"base": base_index, "value": gap, "population": population})
            gap_frame["weighted"] = gap_frame["value"] * gap_frame["population"]
            gap_totals = gap_frame.groupby("base", sort=False)[["weighted", "population"]].sum()
            gap_values = gap_totals["weighted"] / gap_totals["population"].replace(0, np.nan)
            areas["coverage_gap"] = pd.Series(gap_values, index=gap_values.index).reindex(areas.index)
            values = areas["coverage_gap"]
        else:
            vulnerability = pd.to_numeric(areas["vulnerability_score"], errors="coerce")
            values = (
                vulnerability.rank(method="average", pct=True)
                + areas["access_disadvantage"].rank(method="average", pct=True)
            ) / 2.0
            areas["combined_disadvantage"] = values
    areas[map_indicator] = values
    valid = areas.loc[values.notna()].copy()
    if request.scope == "vulnerable":
        vulnerable = np.asarray(demand.get("vulnerable_mask", []), dtype=bool)
        scope_base = np.asarray(
            ctx.get("travel", {}).get("base_index", demand.get("base_index", np.arange(len(vulnerable)))),
            dtype=int,
        )
        if len(vulnerable) and len(scope_base) == len(vulnerable):
            vulnerable_by_area = pd.Series(vulnerable).groupby(scope_base).any()
            valid = valid.loc[valid.index.map(lambda index: bool(vulnerable_by_area.get(index, False)))]
    valid = valid.sort_values(map_indicator, ascending=request.direction == "lowest")
    selected_rows = valid.head(request.limit)
    wgs84 = selected_rows.to_crs("EPSG:4326")
    rows = []
    for rank, (_, row) in enumerate(wgs84.iterrows(), start=1):
        point = row.geometry.representative_point()
        item = {
            "rank": rank,
            "GEOID": str(row.get("GEOID", "")),
            "metric": labels[metric],
            "value": float(row[map_indicator]),
            "longitude": float(point.x), "latitude": float(point.y),
        }
        supporting_fields = {
            "heat_exposure": ("heat_exposure_c",),
            "vulnerability": ("vulnerability_score",),
            "access_disadvantage": ("access_disadvantage",),
            "combined_disadvantage": ("vulnerability_score", "access_disadvantage"),
            "coverage_gap": ("coverage_gap",),
        }[metric]
        for field in supporting_fields:
            source = "heat_exposure" if field == "heat_exposure_c" else field
            item[field] = None if pd.isna(row.get(source)) else float(row[source])
        rows.append(item)
    study = ctx.get("study")
    out = Path(_cfg(ctx, "output").get("dir", "outputs_run")); out.mkdir(parents=True, exist_ok=True)
    map_path = str(out / f"result_query_{query_index + 1}_{metric}.geojson")
    tract_indicator_map(areas, map_indicator, map_path, study.get("boundary"))
    formula = (
        "S_i = 0.5 rank_pct(V_i) + 0.5 rank_pct(D_i)"
        if metric == "combined_disadvantage" else
        "D_i = 1 - max_j[max(0, 1 - d_ij / T)]"
        if metric == "access_disadvantage" else
        "C_i = sum_r(P_ir I[d_ir >= T]) / sum_r(P_ir)"
        if metric == "coverage_gap" else
        vulnerability_formula
        if metric == "vulnerability" else
        "E_i = sum_g(P_g LST_g) / sum_g(P_g)"
    )
    terms = {
        "combined_disadvantage": {
            "V_i": vulnerability_formula,
            "D_i": "one minus the best gradual access to the evaluated facilities",
            "rank_pct": "within-study percentile rank",
        },
        "access_disadvantage": {
            "d_ij": "recorded travel time from demand unit i to candidate j",
            "T": "confirmed travel-time threshold",
        },
        "coverage_gap": {
            "P_ir": "population represented by travel-matrix row r in tract i",
            "I": "1 when no evaluated candidate is reachable inside the threshold",
        },
        "vulnerability": {"R": "within-study percentile normalization", "beta_k": "confirmed indicator weight"},
        "heat_exposure": {"P_g": "population in sub-tract unit g", "LST_g": "Landsat land-surface temperature"},
    }[metric]
    source_fields = {
        "heat_exposure": ["demand.areas.heat_exposure", "demand.areas.heat_exposure_method"],
        "vulnerability": ["demand.areas.vulnerability_score", "vulnerability_analysis.weight_sensitivity"],
        "access_disadvantage": ["travel.matrix", "candidates.cand_id"],
        "combined_disadvantage": ["demand.areas.vulnerability_score", "travel.matrix", "candidates.cand_id"],
        "coverage_gap": ["travel.matrix", "demand.population", "candidates.cand_id"],
    }[metric]
    parameters = {}
    if metric in {"vulnerability", "combined_disadvantage"}:
        parameters.update(weight_scheme=active_scheme, weights=active_weights)
    if metric in {"access_disadvantage", "combined_disadvantage", "coverage_gap"}:
        parameters.update(
            threshold_minutes=float(task.threshold_minutes),
            router=(ctx.get("travel", {}).get("routing_report") or {}).get(
                "router", ctx.get("travel", {}).get("router")
            ),
            facilities="selected" if ctx.has("analysis") else "all retained candidates",
        )
    if metric == "heat_exposure":
        parameters["aggregation_methods"] = heat_methods
    return {
        "raw_text": request.raw_text,
        "metric": metric,
        "metric_label": labels[metric],
        "direction": request.direction,
        "scope": request.scope,
        "top_rows": rows,
        "map": map_path,
        "method": (
            "Ranks the combined within-study vulnerability and access-disadvantage score."
            if metric == "combined_disadvantage" else
            "Ranks one minus the best gradual access available to each tract."
            if metric == "access_disadvantage" else
            "Ranks the population share without a candidate inside the threshold."
            if metric == "coverage_gap" else
            "Ranks the audited tract indicator directly."
        ),
        "computation": {
            "formula": formula,
            "terms": terms,
            "parameters": parameters,
            "source_fields": source_fields,
            "data_basis": (
                heat_basis if metric == "heat_exposure" else
                vulnerability_basis if metric == "vulnerability" else
                access_basis if metric in {"access_disadvantage", "coverage_gap"} else
                f"{vulnerability_basis} {access_basis}"
            ),
        },
    }


@registry.register(
    "answer_result_queries",
    "Answer confirmed read-only result queries over already computed demand, travel, and solution handles.",
)
@_guard("answer_result_queries")
def answer_result_queries(ctx: Context) -> ToolResult:
    """Answer every confirmed result query without changing the primary decision."""
    task = ctx.get("task")
    if not task.result_queries:
        raise ValueError("No result query was confirmed")
    answers = [_answer_result_query(ctx, request, index)
               for index, request in enumerate(task.result_queries)]
    outputs = ctx.get("outputs", {})
    report_path = outputs.get("report") if isinstance(outputs, dict) else None
    if report_path:
        lines = ["\n## Requested result queries\n"]
        for answer in answers:
            lines.extend([f"### {answer['metric_label'].title()}", answer["method"],
                          f"Formula: `{answer['computation']['formula']}`",
                          f"Data basis: {answer['computation']['data_basis']}"])
            for row in answer["top_rows"]:
                lines.append(
                    f"- {row['rank']}. Tract {row['GEOID']}: {answer['metric_label']} = {row['value']:.3f}"
                )
        with Path(report_path).open("a", encoding="utf-8") as report:
            report.write("\n".join(lines) + "\n")
    summary = {"answers": answers, "answered": len(answers)}
    ctx.put("result_query_answers", summary)
    return ToolResult(ok=True, tool="answer_result_queries", handle="result_query_answers", summary=summary)


@registry.register(
    "run_sensitivity_analysis",
    "Solve a confirmed grid of facility counts and access thresholds against the existing travel matrix.",
    {"threshold_minutes": {"type": "array", "items": {"type": "number"}},
     "facility_counts": {"type": "array", "items": {"type": "integer"}},
     "weights": {"type": ["object", "null"]},
     "coverage_group": {"type": "string", "enum": ["all", "vulnerable"]},
     "facility_capacity": {"type": ["number", "null"]}},
)
@_guard("run_sensitivity_analysis")
def run_sensitivity_analysis(
    ctx: Context,
    threshold_minutes: list[float],
    facility_counts: list[int],
    weights: dict | ValueWeights | None = None,
    coverage_group: str = "all",
    facility_capacity: float | None = None,
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
    if facility_capacity is not None and facility_capacity <= 0:
        raise ValueError("facility_capacity must be positive")
    capacities, _ = _candidate_capacities(candidates, facility_capacity)
    if any(value < 1 or value > len(candidate_ids) for value in counts):
        raise ValueError(f"Facility counts must be between 1 and {len(candidate_ids)}")
    task = ctx.get("task", None)
    routed_limit = (travel.get("routing_report") or {}).get("policy_minutes")
    if routed_limit is not None and max(thresholds) > float(routed_limit) + 1e-9:
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
            facility_capacities=capacities,
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
            "population_weighted_access_gini": solution.population_weighted_access_gini,
            "mean_access_score": solution.mean_access_score,
            "assigned_population": solution.assigned_population,
            "maximum_capacity_utilization": solution.maximum_capacity_utilization,
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


@registry.register(
    "compare_value_profiles",
    "Solve two or more confirmed value profiles against one shared candidate and travel baseline.",
    {"profiles": {"type": "array", "items": {"type": "string"}},
     "n_facilities": {"type": "integer"}, "threshold_minutes": {"type": "number"},
     "facility_capacity": {"type": ["number", "null"]}},
)
@_guard("compare_value_profiles")
def compare_value_profiles(
    ctx: Context, profiles: list[str], n_facilities: int = 10,
    threshold_minutes: float = 15, facility_capacity: float | None = None,
) -> ToolResult:
    """Compare confirmed normative profiles without changing the shared spatial inputs."""
    names = list(dict.fromkeys(profiles))
    configured = _cfg(ctx, "value_profiles")
    if len(names) < 2:
        raise ValueError("Choose at least two registered value profiles to compare")
    unknown = [name for name in names if name not in configured]
    if unknown:
        raise ValueError(f"Unknown value profile(s): {', '.join(unknown)}")
    demand, candidates, travel = ctx.get("demand"), ctx.get("candidates"), ctx.get("travel")
    matrix, population, vulnerable, indicators = _decision_inputs(demand, travel)
    candidate_ids = candidates["cand_id"].astype(str).tolist()
    capacities, capacity_evidence = _candidate_capacities(candidates, facility_capacity)
    time_limit = _cfg(ctx, "optimization").get("solver_time_limit_s", 120)
    scenarios = []
    scenario_solutions = []
    for index, name in enumerate(names):
        ctx.progress("Value-profile comparison", index, len(names), unit="profiles")
        weights = ValueWeights(**configured[name]["weights"])
        objective = build_demand_weights(indicators, population, weights)
        solution = solve_mclp(
            matrix, objective, population, vulnerable, n_facilities,
            threshold_minutes, candidate_ids, name, weights, time_limit,
            facility_capacities=capacities,
        )
        if solution.solver_status not in {"Optimal", "Feasible"}:
            raise ValueError(f"Profile {name} produced solver status {solution.solver_status}")
        scenario_solutions.append(solution)
        scenarios.append({
            "profile": name, "profile_label": configured[name].get("label", name),
            "selected_ids": solution.selected_ids,
            "covered_population_ratio": solution.covered_ratio,
            "vulnerable_covered_ratio": solution.vulnerable_covered_ratio,
            "gradual_coverage_ratio": solution.gradual_coverage_ratio,
            "population_weighted_access_gini": solution.population_weighted_access_gini,
            "mean_access_score": solution.mean_access_score,
            "mean_travel_minutes": solution.mean_travel_minutes,
        })
    ctx.progress("Value-profile comparison", len(names), len(names), unit="profiles")
    baseline = scenarios[0]
    comparisons = []
    for alternative in scenarios[1:]:
        base_sites, alternative_sites = set(baseline["selected_ids"]), set(alternative["selected_ids"])
        union = base_sites | alternative_sites
        comparisons.append({
            "baseline": baseline["profile"], "alternative": alternative["profile"],
            "coverage_change": alternative["covered_population_ratio"] - baseline["covered_population_ratio"],
            "vulnerable_coverage_change": alternative["vulnerable_covered_ratio"] - baseline["vulnerable_covered_ratio"],
            "gini_change": (
                alternative["population_weighted_access_gini"] - baseline["population_weighted_access_gini"]
                if alternative["population_weighted_access_gini"] is not None
                and baseline["population_weighted_access_gini"] is not None else None
            ),
            "shared_site_ratio": len(base_sites & alternative_sites) / len(union) if union else 1.0,
            "replaced_facilities": len(base_sites - alternative_sites),
        })
    summary = {
        "shared_baseline": {"n_facilities": n_facilities,
                            "threshold_minutes": threshold_minutes,
                            "mobility_scenario": getattr(ctx.get("task", None), "mobility_scenario", "unknown"),
                            "capacity": capacity_evidence},
        "scenarios": scenarios, "comparisons": comparisons,
    }
    study = ctx.get("study", {})
    if study and "areas" in demand:
        out = Path(_cfg(ctx, "output").get("dir", "outputs_run")) / study["cache_scope"]
        out.mkdir(parents=True, exist_ok=True)
        map_path = str(out / "profile_comparison_map.geojson")
        selections = {
            "baseline": (scenarios[0]["profile_label"], scenario_solutions[0].selected_ids),
            "alternative": (scenarios[1]["profile_label"], scenario_solutions[1].selected_ids),
        }
        comparison_map(
            demand["areas"], candidates, selections, map_path,
            study.get("boundary"), task=ctx.get("task", None),
        )
        summary["map"] = map_path
    ctx.put("scenario_comparison", summary)
    return ToolResult(ok=True, tool="compare_value_profiles", handle="scenario_comparison", summary=summary)

@registry.register("generate_outputs", "Write MapLibre GeoJSON, Pareto PNG, and Markdown report.")
@_guard("generate_outputs")
def generate_outputs(ctx: Context, output_dir: str | None = None, indicator: str = "heat_exposure") -> ToolResult:
    """Write the coverage map, Pareto chart, and Markdown report."""
    study, demand, candidates, analysis = ctx.get("study"), ctx.get("demand"), ctx.get("candidates"), ctx.get("analysis")
    task = ctx.get("task", TaskSpec(place_name=study["place_name"], n_facilities=analysis["n_facilities"],
                                    threshold_minutes=analysis["threshold_minutes"], weights=analysis["solution"].weights_used))
    base = Path(output_dir or _cfg(ctx, "output").get("dir", "outputs_run"))
    out = base if output_dir else base / study["cache_scope"]; out.mkdir(parents=True, exist_ok=True)
    paths = {"map": str(out / "coverage_map.geojson"), "pareto": str(out / "pareto.png"), "report": str(out / "report.md")}
    ctx.progress("Outputs: writing interactive map", 0, 4, unit="artifacts")
    coverage_map(
        demand["areas"], candidates, analysis["solution"], indicator, paths["map"],
        study["boundary"], task=task,
    )
    ctx.progress("Outputs: rendering Pareto chart", 1, 4, unit="artifacts")
    pareto_chart(analysis["points"], analysis["frontier"], paths["pareto"])
    ctx.progress("Outputs: writing method report", 2, 4, unit="artifacts")
    Path(paths["report"]).write_text(render_report(
        task, analysis["solution"], analysis["frontier"], ctx.lineage,
        model_context={
            "capacity_evidence": analysis.get("diagnostics", {}).get("capacity_evidence", {}),
            "built_environment": ctx.get("built_environment", {"status": "not_run"}),
            "population_weighting_threshold": ctx.get("quality", {}).get("heat", {}).get(
                "minimum_valid_population_share", .8
            ),
            "vulnerability": ctx.get("vulnerability_analysis", {}),
        },
    ), encoding="utf-8")
    ctx.progress("Outputs: registering artifacts", 3, 4, unit="artifacts")
    ctx.put("outputs", paths)
    ctx.progress("Outputs ready", 4, 4, unit="artifacts")
    return ToolResult(ok=True, tool="generate_outputs", handle="outputs", summary=paths)


def run_tool_chain(cfg: dict, task: TaskSpec, output_dir: str | None = None, ctx: Context | None = None) -> dict:
    """Execute the exact seven-tool baseline without an LLM."""
    ctx = ctx or Context(); ctx.put("config", cfg); ctx.put("task", task)
    calls = [
        ("resolve_study_area", {"place_name": task.place_name}),
        ("prepare_demand", {}),
        ("prepare_built_environment", {}),
        ("fetch_candidate_sites", {"amenities": task.candidate_amenities}),
        ("build_travel_matrix", {}),
        ("optimize_sites", {"n_facilities": task.n_facilities, "threshold_minutes": task.threshold_minutes,
                            "weights": task.weights.model_dump() if task.weights else None,
                            "coverage_target": task.coverage_target, "coverage_group": task.coverage_group,
                            "facility_capacity": task.facility_capacity,
                            "equity_alpha": task.equity_alpha}),
        ("generate_outputs", {"output_dir": output_dir}),
    ]
    results = []
    for name, args in calls:
        result = registry.get(name).fn(ctx=ctx, **args); results.append(result)
        if not result.ok: return {"ok": False, "failed_tool": name, "results": results, "context": ctx}
    return {"ok": True, "outputs": ctx.get("outputs"), "solution": ctx.get("analysis")["solution"],
            "results": results, "context": ctx}
