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
               weights: ValueWeights | None, time_limit_s: int = 120) -> SiteSolution:
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
    prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit_s))

    chosen = [j for j in range(n_supply) if (x[j].value() or 0) > 0.5]
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
        solver_status=pulp.LpStatus[prob.status],
    )
