"""Deterministic gradual coverage, explicit assignment, capacity, and siting core.

Division of labor: the LLM translates human preferences into ValueWeights;
the solver (PuLP/CBC) does exact, reproducible math. The LLM never touches
numbers here.
"""
from __future__ import annotations
import math
from time import monotonic
import numpy as np
import pulp
from oasis.contracts.domain import SiteSolution, ValueWeights
from oasis.core.optimization.cbc import solve_bounded_cbc


def population_weighted_gini(values: np.ndarray, population: np.ndarray) -> float | None:
    """Return the weighted Gini; undefined when the weighted outcome is uniformly zero."""
    values, population = np.asarray(values, float), np.asarray(population, float)
    if values.shape != population.shape or values.ndim != 1:
        raise ValueError("values and population must be aligned one-dimensional vectors")
    valid = np.isfinite(values) & np.isfinite(population) & (population > 0) & (values >= 0)
    x, w = values[valid], population[valid]
    if not len(x) or w.sum() <= 0 or np.dot(x, w) <= 0:
        return None
    order = np.argsort(x); x, w = x[order], w[order]
    cumulative_w = np.cumsum(w) - w
    cumulative_xw = np.cumsum(x * w) - x * w
    numerator = np.sum(w * (x * cumulative_w - cumulative_xw))
    return float(numerator / (w.sum() * np.dot(x, w)))


def accessibility_metrics(
    served_minutes: np.ndarray, population: np.ndarray, threshold_minutes: float
) -> tuple[float, float | None]:
    """Evaluate population-weighted gradual access and its weighted Gini."""
    served = np.asarray(served_minutes, float)
    score = np.where(
        np.isfinite(served), np.clip(1.0 - served / threshold_minutes, 0.0, 1.0), 0.0
    )
    total = float(np.asarray(population, float).sum())
    mean = float(np.dot(score, population) / total) if total > 0 else 0.0
    return mean, population_weighted_gini(score, population)


def gradual_coverage_matrix(
    travel_matrix: np.ndarray, threshold_minutes: float
) -> np.ndarray:
    """Return linear distance-decayed coverage in [0, 1]."""
    matrix = np.asarray(travel_matrix, dtype=float)
    if matrix.ndim != 2 or threshold_minutes <= 0:
        raise ValueError("travel_matrix must be two-dimensional and threshold positive")
    return np.where(
        np.isfinite(matrix),
        np.clip(1.0 - matrix / threshold_minutes, 0.0, 1.0),
        0.0,
    )


def _capacity_vector(
    capacities: np.ndarray | None, n_supply: int
) -> np.ndarray | None:
    if capacities is None:
        return None
    vector = np.asarray(capacities, dtype=float)
    if vector.shape != (n_supply,) or not np.isfinite(vector).all() or (vector <= 0).any():
        raise ValueError("facility capacities must be positive finite values aligned to candidates")
    return vector


def _solve_gradual_assignment(
    benefit: np.ndarray,
    demand_weights: np.ndarray,
    population: np.ndarray,
    n_facilities: int,
    time_limit_s: int = 120,
    coverage_target: float | None = None,
    coverage_mask: np.ndarray | None = None,
    facility_capacities: np.ndarray | None = None,
    *, cancel_check=None, heartbeat=None,
) -> tuple[list[int], str, np.ndarray]:
    """Select sites and assign splittable demand using distance-decayed benefit."""
    deadline = monotonic() + time_limit_s
    if cancel_check:
        cancel_check()
    benefit = np.asarray(benefit, float)
    demand_weights = np.asarray(demand_weights, float)
    population = np.asarray(population, float)
    if benefit.ndim != 2:
        raise ValueError("benefit must be two-dimensional")
    n_demand, n_supply = benefit.shape
    if demand_weights.shape != (n_demand,) or population.shape != (n_demand,):
        raise ValueError("demand weights and population must match benefit rows")
    if n_facilities < 1 or n_facilities > n_supply:
        raise ValueError("n_facilities must be between 1 and candidate count")
    capacities = _capacity_vector(facility_capacities, n_supply)
    target_mask = np.ones(n_demand, dtype=bool) if coverage_mask is None else np.asarray(coverage_mask, bool)
    if target_mask.shape != (n_demand,) or not target_mask.any():
        raise ValueError("coverage_mask must select at least one aligned demand row")

    problem = pulp.LpProblem("GradualCapacitatedCoverage", pulp.LpMaximize)
    x = [pulp.LpVariable(f"x_{j}", cat="Binary") for j in range(n_supply)]
    eligible = np.argwhere(benefit > 0)
    allocation = {
        (int(i), int(j)): pulp.LpVariable(f"q_{i}_{j}", lowBound=0, upBound=1)
        for i, j in eligible
    }
    problem += pulp.lpSum(
        demand_weights[i] * benefit[i, j] * variable
        for (i, j), variable in allocation.items()
    )
    problem += pulp.lpSum(x) <= n_facilities
    for i in range(n_demand):
        if cancel_check:
            cancel_check()
        if monotonic() >= deadline:
            return [], "Not Solved", np.zeros_like(benefit)
        row = [allocation[(i, j)] for j in range(n_supply) if (i, j) in allocation]
        if row:
            problem += pulp.lpSum(row) <= 1
            for j in range(n_supply):
                if (i, j) in allocation:
                    problem += allocation[(i, j)] <= x[j]
    if capacities is not None:
        for j in range(n_supply):
            column = [
                population[i] * allocation[(i, j)]
                for i in range(n_demand) if (i, j) in allocation
            ]
            if column:
                problem += pulp.lpSum(column) <= capacities[j] * x[j]
    if coverage_target is not None:
        if not 0 <= coverage_target <= 1:
            raise ValueError("coverage_target must be between 0 and 1")
        target_population = population * target_mask
        required = coverage_target * float(target_population.sum())
        served = [
            target_population[i] * allocation[(i, j)]
            for (i, j) in allocation if target_mask[i]
        ]
        problem += pulp.lpSum(served) >= required

    status = solve_bounded_cbc(problem, deadline, cancel_check=cancel_check, heartbeat=heartbeat)
    chosen = [
        j for j in range(n_supply) if (x[j].value() or 0) > .5
    ] if status in {"Optimal", "Feasible"} else []
    assigned = np.zeros_like(benefit)
    if chosen:
        for (i, j), variable in allocation.items():
            assigned[i, j] = max(0.0, float(variable.value() or 0.0))
    return chosen, status, assigned


def _solve_mclp_selection(
    cover: np.ndarray,
    demand_weights: np.ndarray,
    n_facilities: int,
    time_limit_s: int = 120,
    coverage_target: float | None = None,
    coverage_mask: np.ndarray | None = None,
) -> tuple[list[int], str]:
    """Solve the binary coverage model and return selected column indices."""
    deadline = monotonic() + time_limit_s
    cover = np.asarray(cover, dtype=bool)
    demand_weights = np.asarray(demand_weights, dtype=float)
    if cover.ndim != 2:
        raise ValueError("cover must be two-dimensional")
    n_demand, n_supply = cover.shape
    if demand_weights.shape != (n_demand,):
        raise ValueError("demand_weights length must match cover rows")
    if n_facilities < 1 or n_facilities > n_supply:
        raise ValueError("n_facilities must be between 1 and candidate count")
    if coverage_target is not None and not 0 <= coverage_target <= 1:
        raise ValueError("coverage_target must be between 0 and 1")
    target_mask = (
        np.ones(n_demand, dtype=bool)
        if coverage_mask is None
        else np.asarray(coverage_mask, dtype=bool)
    )
    if target_mask.shape != (n_demand,) or not target_mask.any():
        raise ValueError("coverage_mask must select at least one aligned demand row")

    if not np.isfinite(demand_weights).all() or (demand_weights < 0).any():
        raise ValueError("demand_weights must be non-negative and finite")
    objective_rows = demand_weights > 0
    constrained_rows = objective_rows | target_mask if coverage_target is not None else objective_rows
    if not constrained_rows.any():
        return [], "Optimal"

    problem = pulp.LpProblem("MCLP", pulp.LpMaximize)
    x = [pulp.LpVariable(f"x_{j}", cat="Binary") for j in range(n_supply)]
    y = {i: pulp.LpVariable(f"y_{i}", cat="Binary") for i in np.flatnonzero(constrained_rows)}
    problem += pulp.lpSum(demand_weights[i] * y[i] for i in np.flatnonzero(objective_rows))
    problem += pulp.lpSum(x) <= n_facilities
    for i in np.flatnonzero(constrained_rows):
        reachable = np.flatnonzero(cover[i])
        problem += (
            y[i] == 0
            if len(reachable) == 0
            else y[i] <= pulp.lpSum(x[j] for j in reachable)
        )
    if coverage_target is not None:
        required = int(np.ceil(coverage_target * int(target_mask.sum()) - 1e-9))
        problem += pulp.lpSum(y[i] for i in np.flatnonzero(target_mask)) >= required

    status = solve_bounded_cbc(problem, deadline)
    chosen = (
        [j for j in range(n_supply) if (x[j].value() or 0) > 0.5]
        if status in {"Optimal", "Feasible"}
        else []
    )
    return chosen, status


def _site_solution_from_selection(
    travel_matrix: np.ndarray,
    cover: np.ndarray,
    population: np.ndarray,
    equity_population: np.ndarray,
    candidate_ids: list[str],
    chosen: list[int],
    solver_status: str,
    profile_name: str,
    weights: ValueWeights | None,
    threshold_minutes: float,
    assignment: np.ndarray | None = None,
    facility_capacities: np.ndarray | None = None,
) -> SiteSolution:
    """Evaluate one solver selection in the parent process."""
    n_demand = len(population)
    benefit = gradual_coverage_matrix(travel_matrix, threshold_minutes)
    if assignment is None:
        assignment = np.zeros_like(travel_matrix, dtype=float)
        if chosen:
            best = np.argmax(benefit[:, chosen], axis=1)
            for i, local in enumerate(best):
                j = chosen[int(local)]
                if benefit[i, j] > 0:
                    assignment[i, j] = 1.0
    assigned_fraction = np.clip(assignment.sum(axis=1), 0.0, 1.0)
    access_scores = np.sum(benefit * assignment, axis=1)
    covered = assigned_fraction > 1e-9
    served = (
        travel_matrix[:, chosen].min(axis=1)
        if chosen
        else np.full(n_demand, np.inf)
    )
    equity_population = np.asarray(equity_population)
    if equity_population.dtype == bool:
        equity_population = population * equity_population
    equity_population = equity_population.astype(float)
    total_population = max(float(population.sum()), 1.0)
    mean_access = float(np.dot(population, access_scores) / total_population)
    access_gini = population_weighted_gini(access_scores, population)
    loads = np.sum(population[:, None] * assignment, axis=0)
    assigned_people = population[:, None] * assignment
    assigned_total = float(assigned_people.sum())
    finite_travel = np.where(np.isfinite(travel_matrix), travel_matrix, 0.0)
    assigned_mean_travel = (
        float(np.sum(assigned_people * finite_travel) / assigned_total)
        if assigned_total > 0 else -1.0
    )
    capacities = _capacity_vector(facility_capacities, travel_matrix.shape[1])
    load_by_id = {candidate_ids[j]: float(loads[j]) for j in chosen}
    capacity_by_id = (
        {candidate_ids[j]: float(capacities[j]) for j in chosen}
        if capacities is not None else {}
    )
    utilization = (
        max((loads[j] / capacities[j] for j in chosen), default=0.0)
        if capacities is not None else None
    )
    return SiteSolution(
        profile_name=profile_name,
        selected_ids=[candidate_ids[j] for j in chosen],
        covered_population=float(np.dot(population, assigned_fraction)),
        covered_ratio=float(np.dot(population, assigned_fraction) / total_population),
        vulnerable_covered_ratio=float(
            np.dot(equity_population, assigned_fraction) / max(equity_population.sum(), 1)
        ),
        population_weighted_access_gini=access_gini,
        mean_access_score=mean_access,
        gradual_coverage_ratio=mean_access,
        assigned_population=assigned_total,
        facility_loads=load_by_id,
        facility_capacities=capacity_by_id,
        maximum_capacity_utilization=utilization,
        mean_travel_minutes=assigned_mean_travel,
        weights_used=weights,
        solver_status=solver_status,
    )


def build_demand_weights(indicators: dict[str, np.ndarray], population: np.ndarray,
                         weights: ValueWeights) -> np.ndarray:
    """weight_i = population_i * sum_k( w_k * minmax(indicator_ik) ).
    Min-max normalization first, so units (income vs ratios) can't drown
    out the semantic weights."""
    population = np.asarray(population, dtype=float)
    if population.ndim != 1:
        raise ValueError("population must be a one-dimensional array")
    if np.isnan(population).any() or (population < 0).any():
        raise ValueError("population must be non-negative and contain no NaN")

    w = weights.normalized().model_dump()
    # The legacy field name is retained in the public contract, but this
    # component is the unmodified headcount baseline. Multiplying population
    # by normalized density here would double-count density and is not a pure
    # efficiency objective.
    score = np.full_like(population, w.pop("population_density", 0.0), dtype=float)
    for name, wk in w.items():
        if wk == 0 or name not in indicators:
            continue
        x = np.asarray(indicators[name], dtype=float)
        if x.shape != population.shape:
            raise ValueError(
                f"indicator '{name}' shape {x.shape} does not match "
                f"population shape {population.shape}"
            )
        finite = np.isfinite(x)
        if not finite.any():
            continue
        low, high = x[finite].min(), x[finite].max()
        normalized = np.zeros_like(x, dtype=float)
        if high > low:
            normalized[finite] = (x[finite] - low) / (high - low)
        score += wk * normalized
    return population * score


def solve_mclp(travel_matrix: np.ndarray, demand_weights: np.ndarray,
               population: np.ndarray, vulnerable_mask: np.ndarray,
               n_facilities: int, threshold_minutes: float,
               candidate_ids: list[str], profile_name: str,
               weights: ValueWeights | None, time_limit_s: int = 120,
               coverage_target: float | None = None,
               coverage_mask: np.ndarray | None = None,
               facility_capacities: np.ndarray | None = None, *,
               cancel_check=None, heartbeat=None) -> SiteSolution:
    """Solve gradual capacitated coverage with explicit demand assignment."""
    travel_matrix = np.asarray(travel_matrix, dtype=float)
    demand_weights = np.asarray(demand_weights, dtype=float)
    population = np.asarray(population, dtype=float)
    equity_population = np.asarray(vulnerable_mask)
    if travel_matrix.ndim != 2:
        raise ValueError("travel_matrix must be two-dimensional")
    n_demand, n_supply = travel_matrix.shape
    if demand_weights.shape != (n_demand,):
        raise ValueError("demand_weights length must match travel_matrix rows")
    if population.shape != (n_demand,):
        raise ValueError("population length must match travel_matrix rows")
    if equity_population.shape != (n_demand,):
        raise ValueError("equity population length must match travel_matrix rows")
    if equity_population.dtype != bool and (
        not np.isfinite(equity_population.astype(float)).all()
        or (equity_population.astype(float) < 0).any()
    ):
        raise ValueError("equity population must be non-negative and finite")
    if len(candidate_ids) != n_supply:
        raise ValueError("candidate_ids length must match travel_matrix columns")
    if n_facilities < 1 or n_facilities > n_supply:
        raise ValueError("n_facilities must be between 1 and candidate count")
    if threshold_minutes <= 0:
        raise ValueError("threshold_minutes must be positive")
    if coverage_target is not None and not 0 <= coverage_target <= 1:
        raise ValueError("coverage_target must be between 0 and 1")
    target_mask = np.ones(n_demand, dtype=bool) if coverage_mask is None else np.asarray(coverage_mask, dtype=bool)
    if target_mask.shape != (n_demand,) or not target_mask.any():
        raise ValueError("coverage_mask must select at least one aligned demand row")
    if np.isnan(travel_matrix).any() or (travel_matrix < 0).any():
        raise ValueError("travel_matrix contains NaN or negative values")
    if np.isnan(demand_weights).any() or (demand_weights < 0).any():
        raise ValueError("demand_weights must be non-negative and contain no NaN")

    cover = travel_matrix <= threshold_minutes
    benefit = gradual_coverage_matrix(travel_matrix, threshold_minutes)
    chosen, solver_status, assignment = _solve_gradual_assignment(
        benefit,
        demand_weights,
        population,
        n_facilities,
        time_limit_s,
        coverage_target,
        target_mask,
        facility_capacities,
        cancel_check=cancel_check,
        heartbeat=heartbeat,
    )
    # Solver y values can be arbitrary for zero-weight rows. Always derive
    # reported coverage from the selected geography instead.
    return _site_solution_from_selection(
        travel_matrix,
        cover,
        population,
        equity_population,
        candidate_ids,
        chosen,
        solver_status,
        profile_name,
        weights,
        threshold_minutes,
        assignment,
        facility_capacities,
    )

def solve_lscp(travel_matrix: np.ndarray, population: np.ndarray, vulnerable_mask: np.ndarray,
               threshold_minutes: float, candidate_ids: list[str], coverage_target: float = 1.0,
               coverage_mask: np.ndarray | None = None, time_limit_s: float = 120, *,
               cancel_check=None, heartbeat=None) -> SiteSolution:
    """Minimize facility count within a wall-clock budget, including model export."""
    deadline = monotonic() + time_limit_s
    if cancel_check:
        cancel_check()
    travel_matrix = np.asarray(travel_matrix, dtype=float); population = np.asarray(population, dtype=float)
    equity_population = np.asarray(vulnerable_mask)
    if travel_matrix.ndim != 2: raise ValueError("travel_matrix must be two-dimensional")
    n_demand, n_supply = travel_matrix.shape
    if population.shape != (n_demand,) or equity_population.shape != (n_demand,):
        raise ValueError("population and vulnerable_mask must match travel_matrix rows")
    if len(candidate_ids) != n_supply or n_supply == 0: raise ValueError("candidate_ids must match non-empty matrix columns")
    if threshold_minutes < 0 or not 0 <= coverage_target <= 1: raise ValueError("invalid threshold or coverage target")
    target_mask = np.ones(n_demand, dtype=bool) if coverage_mask is None else np.asarray(coverage_mask, dtype=bool)
    if target_mask.shape != (n_demand,) or not target_mask.any():
        raise ValueError("coverage_mask must select at least one aligned demand row")
    cover = travel_matrix <= threshold_minutes
    problem = pulp.LpProblem("LSCP", pulp.LpMinimize)
    x = [pulp.LpVariable(f"x_{j}", cat="Binary") for j in range(n_supply)]
    # Non-target y variables can always be zero: dropping them is exact.
    target_rows = np.flatnonzero(target_mask)
    y = {i: pulp.LpVariable(f"y_{i}", cat="Binary") for i in target_rows}
    problem += pulp.lpSum(x)
    status = "Not Solved"
    for i in target_rows:
        if cancel_check:
            cancel_check()
        if monotonic() >= deadline:
            break
        reachable = np.flatnonzero(cover[i])
        problem += (y[i] == 0) if len(reachable) == 0 else (
            y[i] <= pulp.LpAffineExpression((x[j], 1) for j in reachable)
        )
    else:
        required = int(np.ceil(coverage_target * int(target_mask.sum()) - 1e-9))
        problem += pulp.lpSum(y.values()) >= required
        status = solve_bounded_cbc(problem, deadline, cancel_check=cancel_check, heartbeat=heartbeat)
    chosen = ([j for j in range(n_supply) if (x[j].value() or 0) > .5]
              if status in {"Optimal", "Feasible"} else [])
    covered = cover[:, chosen].any(axis=1) if chosen else np.zeros(n_demand, dtype=bool)
    served = travel_matrix[:, chosen].min(axis=1) if chosen else np.full(n_demand, np.inf)
    covered_times = served[covered & np.isfinite(served)]
    mean_access, access_gini = accessibility_metrics(served, population, threshold_minutes)
    return SiteSolution(profile_name="minimum_facilities", selected_ids=[candidate_ids[j] for j in chosen],
                        covered_population=float(population[covered].sum()),
                        covered_ratio=float(population[covered].sum() / max(population.sum(), 1)),
                        vulnerable_covered_ratio=float(
                            (population * equity_population)[covered].sum() / max((population * equity_population).sum(), 1)
                            if equity_population.dtype == bool else equity_population[covered].sum() / max(equity_population.sum(), 1)
                        ),
                        population_weighted_access_gini=access_gini,
                        mean_access_score=mean_access,
                        mean_travel_minutes=float(np.mean(covered_times) if covered_times.size else -1),
                        weights_used=None, solver_status=status)


def minimum_coverage_threshold(travel_matrix: np.ndarray, population: np.ndarray,
                               vulnerable_mask: np.ndarray, candidate_ids: list[str],
                               n_facilities: int, coverage_target: float = 1.0,
                               coverage_mask: np.ndarray | None = None,
                               time_limit_s: int = 120, progress_callback=None, *,
                               total_time_limit_s: float | None = None,
                               search_diagnostics: dict | None = None,
                               cancel_check=None) -> tuple[float | None, SiteSolution | None]:
    """Find the smallest feasible observed threshold within one bounded search budget."""
    matrix = np.asarray(travel_matrix, dtype=float)
    if matrix.ndim != 2 or n_facilities < 1:
        raise ValueError("n_facilities must be positive and travel_matrix must be two-dimensional")
    values = np.unique(matrix[np.isfinite(matrix)])
    if not len(values):
        if search_diagnostics is not None:
            search_diagnostics.update(status="no_finite_travel_times", probes=0)
        return None, None
    expected_probes = max(1, int(np.ceil(np.log2(len(values) + 1))))
    total_budget = float(total_time_limit_s if total_time_limit_s is not None else time_limit_s)
    if total_budget <= 0:
        raise ValueError("total_time_limit_s must be positive")
    started = monotonic()
    low, high, best, solution, probes = 0, len(values) - 1, None, None, 0
    status = "complete"
    while low <= high:
        if cancel_check:
            cancel_check()
        remaining = total_budget - (monotonic() - started)
        if remaining <= 0:
            status = "time_limit"
            break
        middle = (low + high) // 2
        threshold = float(values[middle])
        remaining_probes = max(1, expected_probes - probes)
        probe_budget = min(remaining, max(1, min(float(time_limit_s), math.ceil(remaining / remaining_probes))))
        if progress_callback:
            progress_callback("Minimum-threshold search", probes, expected_probes)
        current = solve_lscp(
            matrix, population, vulnerable_mask, threshold, candidate_ids,
            coverage_target, coverage_mask, probe_budget,
            cancel_check=cancel_check,
            heartbeat=(lambda: progress_callback("Minimum-threshold search", probes, expected_probes))
            if progress_callback else None,
        )
        probes += 1
        if progress_callback:
            progress_callback("Minimum-threshold search", probes, expected_probes)
        if current.solver_status not in {"Optimal", "Feasible", "Infeasible"}:
            status = "solver_time_limit"
            break
        feasible = (
            current.solver_status in {"Optimal", "Feasible"}
            and len(current.selected_ids) <= n_facilities
        )
        if feasible:
            best, solution, high = threshold, current, middle - 1
        elif current.solver_status == "Feasible":
            # An incumbent above budget does not prove that <= budget is impossible.
            status = "solver_time_limit"
            break
        else:
            low = middle + 1
    elapsed = monotonic() - started
    if search_diagnostics is not None:
        search_diagnostics.update(
            status=status,
            probes=probes,
            expected_probes=expected_probes,
            elapsed_seconds=round(elapsed, 3),
            total_time_limit_seconds=total_budget,
            best_threshold_minutes=best,
        )
    return best, solution
