"""Explicit post-paper, approximate planning over a finite expanded candidate set."""
from pathlib import Path
from time import monotonic

import geopandas as gpd
import numpy as np
import pandas as pd

from oasis.agent.context import Context
from oasis.contracts.domain import Provenance
from oasis.core.outputs.maps import coverage_map
from oasis.core.optimization.models import solve_lscp
from oasis.core.spatial.accessibility import haversine_matrix


def plan_hypothetical_sites(ctx: Context, target_mask: np.ndarray, time_limit_s: float) -> dict:
    """Minimize total sites after adding one hypothetical location per target tract.

    Both existing and hypothetical sites use the same declared Haversine model.
    No synthetic duration is inserted into an ORS matrix. Optimality applies only
    to this finite expanded set, not to unrestricted placement or real access.
    """
    task, demand, existing = (ctx.get(key) for key in ("task", "demand", "candidates"))
    deadline = monotonic() + time_limit_s
    config = ctx.get("config", {})
    study = ctx.get("study", {})
    summary = {
        "extension": "post_paper_hypothetical_sites", "accuracy": "approximate_haversine",
        "threshold_minutes": task.threshold_minutes,
        "coverage_target": task.coverage_target if task.coverage_target is not None else 1.0,
        "coverage_group": task.coverage_group,
        "interpretation": "Minimum total facilities in the finite expanded candidate set under approximate Haversine travel. Hypothetical locations are tract demand centres, not verified premises, entrances, or buildable land. No ORS network guarantee.",
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
    ctx.progress("Hypothetical-site extension (approximate)", 0, 1)
    solution = solve_lscp(matrix, population, np.asarray(demand["vulnerable_mask"]),
                          task.threshold_minutes, candidates.cand_id.astype(str).tolist(),
                          summary["coverage_target"], target_mask, max(0, deadline - monotonic()),
                          cancel_check=ctx.check_cancelled,
                          heartbeat=lambda: ctx.progress("Hypothetical-site extension (approximate)", 0, 1))
    feasible = solution.solver_status in {"Optimal", "Feasible"}
    selected = candidates.loc[candidates.cand_id.astype(str).isin(solution.selected_ids)]
    summary.update(
        feasible=feasible, solver_status=solution.solver_status,
        minimum_proven=solution.solver_status == "Optimal",
        minimum_facilities=len(selected) if solution.solver_status == "Optimal" else None,
        feasible_facility_count=len(selected) if feasible else None,
        existing_selected_count=int((~selected.is_hypothetical).sum()),
        hypothetical_selected_count=int(selected.is_hypothetical.sum()),
        generated_candidate_count=len(additions), existing_candidate_count=len(existing),
        speed_kmh=speed, detour_factor=detour, selected_ids=solution.selected_ids,
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
    ctx.progress("Hypothetical-site extension (approximate)", 1, 1)
    ctx.add_provenance(Provenance(
        source="Post-paper hypothetical-site planning: generated tract demand centres (not observed venues)",
        record_count=len(additions),
        notes=f"Explicit opt-in. Finite-set LSCP; Haversine speed={speed:g} km/h, detour={detour:g}. No network, land-suitability, access, or building verification.",
    ))
    return summary
