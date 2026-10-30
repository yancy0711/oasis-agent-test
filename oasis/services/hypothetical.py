"""Explicit post-paper, approximate planning over a finite expanded candidate set."""
from pathlib import Path
from time import monotonic
from copy import deepcopy

import geopandas as gpd
import numpy as np
import pandas as pd

from oasis.agent.context import Context
from oasis.contracts.domain import Provenance
from oasis.core.outputs.maps import coverage_map
from oasis.core.optimization.models import build_demand_weights, solve_lscp, solve_mclp
from oasis.core.spatial.accessibility import haversine_matrix


def plan_hypothetical_sites(ctx: Context, target_mask: np.ndarray, time_limit_s: float) -> dict:
    """Solve a fixed-budget portfolio or minimize count over generated candidates.

    Both existing and hypothetical sites use the same declared Haversine model.
    No synthetic duration is inserted into an ORS matrix. Optimality applies only
    to this finite expanded set, not to unrestricted placement or real access.
    """
    task, demand, existing = (ctx.get(key) for key in ("task", "demand", "candidates"))
    deadline = monotonic() + time_limit_s
    config = ctx.get("config", {})
    study = ctx.get("study", {})
    fixed_budget = task.analysis_goal == "facility_siting"
    summary = {
        "extension": "post_paper_hypothetical_sites", "accuracy": "approximate_haversine",
        "threshold_minutes": task.threshold_minutes,
        "coverage_target": task.coverage_target if fixed_budget or task.coverage_target is not None else 1.0,
        "coverage_group": task.coverage_group,
        "planning_mode": "fixed_budget" if fixed_budget else "minimum_facilities",
        "facility_budget": task.n_facilities if fixed_budget else None,
        "interpretation": ("Fixed-budget siting with the confirmed value weights and coverage requirement. " if fixed_budget else "Minimum total facilities in the finite expanded candidate set. ") + "Approximate Haversine travel; hypothetical locations are tract demand centres, not verified premises, entrances, or buildable land. No ORS network guarantee.",
    }
    target_rows = np.flatnonzero(target_mask)
    if len(target_rows) > 250 or time_limit_s <= 0:
        return {**summary, "feasible": False, "solver_status": "Not Solved",
                "reason": "This exploratory extension supports up to 250 target tracts and requires remaining solver time."}
    points = demand["points"].to_crs(4326)
    additions = gpd.GeoDataFrame({
        "cand_id": [f"hypothetical_tract_{demand['areas'].iloc[i]['GEOID']}" for i in target_rows],
        "name": [f"Hypothetical site - tract {demand['areas'].iloc[i]['GEOID']}" for i in target_rows],
        "is_hypothetical": True,
    }, geometry=points.geometry.iloc[target_rows].reset_index(drop=True), crs=4326)
    if study.get("boundary") is not None:
        additions = additions.loc[additions.geometry.covered_by(study["boundary"].to_crs(4326).geometry.union_all())]
    # Use a fresh table; the original candidates and routing evidence stay intact.
    existing = existing.to_crs(4326).copy()
    existing["is_hypothetical"] = False
    candidates = gpd.GeoDataFrame(pd.concat([existing, additions], ignore_index=True), geometry="geometry", crs=4326)
    accessibility = config.get("accessibility", {})
    mode = task.mobility_scenario
    speed = accessibility.get("mode_speeds_kmh", {}).get(mode, {"walking": 4.8, "cycling": 15, "driving": 35}[mode])
    detour = accessibility.get("mode_detour_factors", {}).get(mode, {"walking": 1.3, "cycling": 1.2, "driving": 1.25}[mode])
    matrix = haversine_matrix(points, candidates, speed, detour)
    population = np.asarray(demand["population"], dtype=float)
    equity_population = np.asarray(demand.get("equity_population", demand["vulnerable_mask"]))
    ctx.progress("Hypothetical-site extension (approximate)", 0, 1)
    if fixed_budget:
        objective = (build_demand_weights(demand.get("indicators", {}), population, task.weights)
                     if task.weights else population / max(population.sum(), 1))
        solution = solve_mclp(
            matrix, objective, population, equity_population,
            min(task.n_facilities, len(candidates)), task.threshold_minutes, candidates.cand_id.astype(str).tolist(),
            "hypothetical_fixed_budget", task.weights, max(0, deadline - monotonic()),
            coverage_target=task.coverage_target, coverage_mask=target_mask,
            cancel_check=ctx.check_cancelled,
            heartbeat=lambda: ctx.progress("Hypothetical fixed-budget siting (approximate)", 0, 1),
        )
    else:
        solution = solve_lscp(matrix, population, equity_population,
                              task.threshold_minutes, candidates.cand_id.astype(str).tolist(),
                              summary["coverage_target"], target_mask, max(0, deadline - monotonic()),
                              cancel_check=ctx.check_cancelled,
                              heartbeat=lambda: ctx.progress("Hypothetical-site extension (approximate)", 0, 1))
    feasible = solution.solver_status in {"Optimal", "Feasible"}
    selected = candidates.loc[candidates.cand_id.astype(str).isin(solution.selected_ids)]
    summary.update(
        feasible=feasible, solver_status=solution.solver_status,
        minimum_proven=not fixed_budget and solution.solver_status == "Optimal",
        objective_optimal=solution.solver_status == "Optimal",
        minimum_facilities=len(selected) if not fixed_budget and solution.solver_status == "Optimal" else None,
        feasible_facility_count=len(selected) if feasible else None,
        existing_selected_count=int((~selected.is_hypothetical).sum()),
        hypothetical_selected_count=int(selected.is_hypothetical.sum()),
        generated_candidate_count=len(additions), existing_candidate_count=len(existing),
        speed_kmh=speed, detour_factor=detour, selected_ids=solution.selected_ids,
        value_weights=task.weights.model_dump(mode="json") if task.weights else None,
        metrics={"coverage": solution.covered_ratio, "vulnerable": solution.vulnerable_covered_ratio,
                 "population_weighted_access_gini": solution.population_weighted_access_gini,
                 "mean_access_score": solution.mean_access_score, "gradual_coverage": solution.gradual_coverage_ratio,
                 "assigned_population": solution.assigned_population, "avg_walk": solution.mean_travel_minutes} if feasible else {},
        selected_sites=[{"id": str(row.cand_id), "name": str(row["name"]) if pd.notna(row.get("name")) else str(row.cand_id),
                         "is_hypothetical": bool(row.is_hypothetical),
                         "latitude": float(row.geometry.y), "longitude": float(row.geometry.x)}
                        for _, row in selected.iterrows()],
    )
    if feasible and study:
        path = Path(config.get("output", {}).get("dir", "outputs_run")) / study["cache_scope"] / "hypothetical_coverage_map.geojson"
        path.parent.mkdir(parents=True, exist_ok=True)
        coverage_map(demand["areas"], candidates, solution, "heat_exposure", str(path), study.get("boundary"), task=task)
        summary["map"] = str(path)
    if fixed_budget and not feasible and task.coverage_target is not None:
        from oasis.agent.recovery import coverage_recovery
        from oasis.services.workflow import find_minimum_facilities, find_minimum_threshold
        from oasis.contracts.domain import RecoveryProposal
        if solution.solver_status == "Infeasible":
            # Reuse the recovery model on an isolated expanded candidate context;
            # never compute repair advice against the observed-only matrix.
            repair = Context()
            repair.cancel_callback = ctx.cancel_callback
            repair.progress_callback = ctx.progress_callback
            repair_cfg = deepcopy(config)
            repair_cfg.setdefault("optimization", {})["solver_time_limit_s"] = max(0, (deadline - monotonic()) / 2)
            for key, value in {"task": task, "config": repair_cfg, "demand": demand, "candidates": candidates,
                               "travel": {"matrix": matrix, "router": "haversine"}}.items():
                repair.put(key, value)
            count = find_minimum_facilities(repair, threshold_minutes=task.threshold_minutes,
                                            coverage_target=task.coverage_target, coverage_group=task.coverage_group).summary
            repair_cfg["optimization"]["minimum_threshold_total_time_limit_s"] = max(0, deadline - monotonic())
            threshold = find_minimum_threshold(repair, n_facilities=task.n_facilities,
                                              coverage_target=task.coverage_target, coverage_group=task.coverage_group).summary
            recovery = coverage_recovery(task, count, threshold,
                "The fixed-budget hypothetical-site plan is infeasible in the expanded finite candidate set. Confirm a single-input repair; the coverage requirement is unchanged.", "approximate")
        else:
            recovery = RecoveryProposal(
                diagnosis="The hypothetical-site optimization reached its time limit without a verified solution.",
                question="Retry the same task. No budget, threshold, or coverage-target change has been applied.",
            )
        summary["recovery"] = recovery.model_dump(mode="json")
    ctx.progress("Hypothetical-site extension (approximate)", 1, 1)
    ctx.add_provenance(Provenance(
        source="Post-paper hypothetical-site planning: generated tract demand centres (not observed venues)",
        record_count=len(additions),
        notes=f"Explicit opt-in. Mode={summary['planning_mode']}; Haversine speed={speed:g} km/h, detour={detour:g}. No network, land-suitability, access, or building verification.",
    ))
    return summary
