"""Deterministic optimization core: MCLP siting + Pareto trade-off sweep.

Division of labor: the LLM translates human preferences into ValueWeights;
the solver (PuLP/CBC) does exact, reproducible math. The LLM never touches
numbers here.
"""
from __future__ import annotations
import numpy as np
import pulp
from agent.schemas import SiteSolution, ValueWeights


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
    score = np.zeros_like(population, dtype=float)
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
               coverage_mask: np.ndarray | None = None) -> SiteSolution:
    """Maximal Covering Location Problem:
        max  sum_i weight_i * y_i
        s.t. sum_j x_j <= N;  y_i <= sum_{j in C_i} x_j;  x,y binary
    Demand points with no reachable candidate get y_i == 0 explicitly --
    leaving that to the solver produces hard-to-diagnose infeasibility."""
    travel_matrix = np.asarray(travel_matrix, dtype=float)
    demand_weights = np.asarray(demand_weights, dtype=float)
    population = np.asarray(population, dtype=float)
    vulnerable_mask = np.asarray(vulnerable_mask, dtype=bool)
    if travel_matrix.ndim != 2:
        raise ValueError("travel_matrix must be two-dimensional")
    n_demand, n_supply = travel_matrix.shape
    if demand_weights.shape != (n_demand,):
        raise ValueError("demand_weights length must match travel_matrix rows")
    if population.shape != (n_demand,):
        raise ValueError("population length must match travel_matrix rows")
    if vulnerable_mask.shape != (n_demand,):
        raise ValueError("vulnerable_mask length must match travel_matrix rows")
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

    prob = pulp.LpProblem("MCLP", pulp.LpMaximize)
    x = [pulp.LpVariable(f"x_{j}", cat="Binary") for j in range(n_supply)]
    y = [pulp.LpVariable(f"y_{i}", cat="Binary") for i in range(n_demand)]
    prob += pulp.lpSum(demand_weights[i] * y[i] for i in range(n_demand))
    prob += pulp.lpSum(x) <= n_facilities
    for i in range(n_demand):
        js = np.flatnonzero(cover[i])
        prob += (y[i] == 0) if len(js) == 0 else (y[i] <= pulp.lpSum(x[j] for j in js))
    if coverage_target is not None:
        required = int(np.ceil(coverage_target * int(target_mask.sum()) - 1e-9))
        prob += pulp.lpSum(y[i] for i in np.flatnonzero(target_mask)) >= required
    prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit_s))

    solver_status = pulp.LpStatus[prob.status]
    chosen = ([j for j in range(n_supply) if (x[j].value() or 0) > 0.5]
              if solver_status in {"Optimal", "Feasible"} else [])
    # Report geographic coverage from chosen sites. Solver y values can be
    # arbitrary for zero-weight rows and must not define evaluation metrics.
    covered = (
        cover[:, chosen].any(axis=1)
        if chosen
        else np.zeros(n_demand, dtype=bool)
    )
    served = travel_matrix[:, chosen].min(axis=1) if chosen else np.full(n_demand, np.inf)
    covered_times = served[covered & np.isfinite(served)]

    return SiteSolution(
        profile_name=profile_name,
        selected_ids=[candidate_ids[j] for j in chosen],
        covered_population=float(population[covered].sum()),
        covered_ratio=float(population[covered].sum() / max(population.sum(), 1)),
        vulnerable_covered_ratio=float(
            population[covered & vulnerable_mask].sum()
            / max(population[vulnerable_mask].sum(), 1)),
        mean_travel_minutes=float(
            np.mean(covered_times) if covered_times.size else -1
        ),
        weights_used=weights,
        solver_status=solver_status,
    )

def solve_lscp(travel_matrix: np.ndarray, population: np.ndarray, vulnerable_mask: np.ndarray,
               threshold_minutes: float, candidate_ids: list[str], coverage_target: float = 1.0,
               coverage_mask: np.ndarray | None = None, time_limit_s: int = 120) -> SiteSolution:
    """Minimize facility count while meeting a confirmed tract-coverage target."""
    travel_matrix = np.asarray(travel_matrix, dtype=float); population = np.asarray(population, dtype=float)
    vulnerable_mask = np.asarray(vulnerable_mask, dtype=bool)
    if travel_matrix.ndim != 2: raise ValueError("travel_matrix must be two-dimensional")
    n_demand, n_supply = travel_matrix.shape
    if population.shape != (n_demand,) or vulnerable_mask.shape != (n_demand,):
        raise ValueError("population and vulnerable_mask must match travel_matrix rows")
    if len(candidate_ids) != n_supply or n_supply == 0: raise ValueError("candidate_ids must match non-empty matrix columns")
    if threshold_minutes < 0 or not 0 <= coverage_target <= 1: raise ValueError("invalid threshold or coverage target")
    target_mask = np.ones(n_demand, dtype=bool) if coverage_mask is None else np.asarray(coverage_mask, dtype=bool)
    if target_mask.shape != (n_demand,) or not target_mask.any():
        raise ValueError("coverage_mask must select at least one aligned demand row")
    cover = travel_matrix <= threshold_minutes
    problem = pulp.LpProblem("LSCP", pulp.LpMinimize)
    x = [pulp.LpVariable(f"x_{j}", cat="Binary") for j in range(n_supply)]
    y = [pulp.LpVariable(f"y_{i}", cat="Binary") for i in range(n_demand)]
    problem += pulp.lpSum(x)
    for i in range(n_demand):
        reachable = np.flatnonzero(cover[i])
        problem += (y[i] == 0) if len(reachable) == 0 else (y[i] <= pulp.lpSum(x[j] for j in reachable))
    required = int(np.ceil(coverage_target * int(target_mask.sum()) - 1e-9))
    problem += pulp.lpSum(y[i] for i in np.flatnonzero(target_mask)) >= required
    problem.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit_s))
    status = pulp.LpStatus[problem.status]
    chosen = ([j for j in range(n_supply) if (x[j].value() or 0) > .5]
              if status in {"Optimal", "Feasible"} else [])
    covered = cover[:, chosen].any(axis=1) if chosen else np.zeros(n_demand, dtype=bool)
    served = travel_matrix[:, chosen].min(axis=1) if chosen else np.full(n_demand, np.inf)
    covered_times = served[covered & np.isfinite(served)]
    return SiteSolution(profile_name="minimum_facilities", selected_ids=[candidate_ids[j] for j in chosen],
                        covered_population=float(population[covered].sum()),
                        covered_ratio=float(population[covered].sum() / max(population.sum(), 1)),
                        vulnerable_covered_ratio=float(population[covered & vulnerable_mask].sum() /
                                                       max(population[vulnerable_mask].sum(), 1)),
                        mean_travel_minutes=float(np.mean(covered_times) if covered_times.size else -1),
                        weights_used=None, solver_status=status)


def minimum_coverage_threshold(travel_matrix: np.ndarray, population: np.ndarray,
                               vulnerable_mask: np.ndarray, candidate_ids: list[str],
                               n_facilities: int, coverage_target: float = 1.0,
                               coverage_mask: np.ndarray | None = None,
                               time_limit_s: int = 120, progress_callback=None) -> tuple[float | None, SiteSolution | None]:
    """Find the smallest observed travel time at which LSCP needs no more than the budget."""
    matrix = np.asarray(travel_matrix, dtype=float)
    if matrix.ndim != 2 or not 1 <= n_facilities <= matrix.shape[1]:
        raise ValueError("n_facilities must be between 1 and candidate count")
    values = np.unique(matrix[np.isfinite(matrix)])
    if not len(values): return None, None
    low, high, best, solution, probes = 0, len(values) - 1, None, None, 0
    while low <= high:
        middle = (low + high) // 2; threshold = float(values[middle]); probes += 1
        current = solve_lscp(matrix, population, vulnerable_mask, threshold, candidate_ids,
                             coverage_target, coverage_mask, time_limit_s)
        feasible = current.solver_status in {"Optimal", "Feasible"} and len(current.selected_ids) <= n_facilities
        if progress_callback: progress_callback("Minimum-threshold search", probes, max(1, int(np.ceil(np.log2(len(values) + 1)))))
        if feasible: best, solution, high = threshold, current, middle - 1
        else: low = middle + 1
    return best, solution
