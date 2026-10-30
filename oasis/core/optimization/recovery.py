"""Feasible, rounded threshold alternatives under the original siting model."""
from time import monotonic

import numpy as np

from oasis.core.optimization.models import solve_mclp


def minimum_assignment_threshold(matrix, population, equity_population, candidate_ids, n_facilities,
                                 coverage_target, coverage_mask, facility_capacities, time_limit_s,
                                 policy_limit=None, *, cancel_check=None, progress=None):
    """Search 0.1-minute settings strictly above route durations, retaining verified incumbents."""
    started = monotonic()
    values = np.unique(np.floor(matrix[np.isfinite(matrix)] * 10) / 10 + .1)
    if policy_limit is not None:
        values = values[values <= policy_limit + 1e-9]
    low, high, best, solution, probes = 0, len(values) - 1, None, None, 0
    status = "complete"
    if n_facilities > len(candidate_ids):
        n_facilities = len(candidate_ids)  # The budget is an upper bound.
    while low <= high:
        if cancel_check:
            cancel_check()
        remaining = time_limit_s - (monotonic() - started)
        if remaining <= 0:
            status = "time_limit"
            break
        middle = (low + high) // 2
        threshold = round(float(values[middle]), 1)
        probes += 1
        def heartbeat():
            if progress:
                progress("Checking threshold alternative", probes, probes + int(np.ceil(np.log2(high-low+2))))
        heartbeat()
        current = solve_mclp(
            matrix, population, population, equity_population, n_facilities, threshold,
            candidate_ids, "verified_recovery", None, min(remaining, max(1, remaining / max(1, np.ceil(np.log2(high-low+2))))),
            coverage_target=coverage_target, coverage_mask=coverage_mask, facility_capacities=facility_capacities,
            cancel_check=cancel_check, heartbeat=heartbeat, feasibility_only=True,
        )
        if current.solver_status in {"Optimal", "Feasible"}:
            best, solution, high = threshold, current, middle - 1
        elif current.solver_status == "Infeasible":
            low = middle + 1
        else:
            status = "time_limit"
            break
    if best is None and status == "complete" and policy_limit is not None:
        status = "routing_scope_limited"
    return best, solution, {"status": status, "probes": probes,
                             "elapsed_seconds": round(monotonic() - started, 3),
                             "resolution_minutes": .1}
