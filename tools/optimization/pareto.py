"""Pareto trade-off sweep (HITL mechanism #3): re-solve MCLP along the
efficiency<->equity interpolation line. This chart is the paper's
centerpiece and the live demo's money shot.
"""
from __future__ import annotations
import numpy as np
from agent.schemas import ParetoPoint, ValueWeights
from tools.optimization.models import build_demand_weights, solve_mclp


def interpolate(a: ValueWeights, b: ValueWeights, alpha: float) -> ValueWeights:
    da, db = a.normalized().model_dump(), b.normalized().model_dump()
    return ValueWeights(**{k: (1 - alpha) * da[k] + alpha * db[k] for k in da})


def pareto_sweep(travel_matrix, indicators, population, vulnerable_mask,
                 candidate_ids, w_efficiency: ValueWeights, w_equity: ValueWeights,
                 n_facilities: int, threshold_minutes: float,
                 steps: int = 11) -> list[ParetoPoint]:
    """Re-solve MCLP along the efficiency<->equity interpolation line.
    This chart is the paper's centerpiece and the live demo's money shot."""
    points = []
    for alpha in np.linspace(0, 1, steps):
        w = interpolate(w_efficiency, w_equity, float(alpha))
        dw = build_demand_weights(indicators, population, w)
        sol = solve_mclp(travel_matrix, dw, population, vulnerable_mask,
                         n_facilities, threshold_minutes, candidate_ids,
                         profile_name=f"alpha={alpha:.2f}", weights=w)
        points.append(ParetoPoint(alpha=float(alpha),
                                  efficiency_metric=sol.covered_ratio,
                                  equity_metric=sol.vulnerable_covered_ratio,
                                  solution=sol))
    return points


def coverage_pareto_sweep(travel_matrix, population, vulnerable_mask,
                          candidate_ids, n_facilities: int,
                          threshold_minutes: float,
                          steps: int = 11) -> list[ParetoPoint]:
    '''Sweep total versus vulnerable population coverage directly.'''
    population = np.asarray(population, dtype=float)
    vulnerable_mask = np.asarray(vulnerable_mask, dtype=bool)
    if population.ndim != 1 or vulnerable_mask.shape != population.shape:
        raise ValueError('population and vulnerable_mask must be aligned vectors')
    if np.isnan(population).any() or (population < 0).any() or population.sum() <= 0:
        raise ValueError('population must be non-negative, finite, and have positive total')
    vulnerable_population = population * vulnerable_mask
    if vulnerable_population.sum() <= 0:
        raise ValueError('vulnerable_mask must contain positive-population demand')
    if steps < 2:
        raise ValueError('steps must be at least 2')

    efficiency = population / population.sum()
    equity = vulnerable_population / vulnerable_population.sum()
    points = []
    for alpha in np.linspace(0, 1, steps):
        alpha = float(alpha)
        demand_weights = (1 - alpha) * efficiency + alpha * equity
        solution = solve_mclp(
            travel_matrix, demand_weights, population, vulnerable_mask,
            n_facilities, threshold_minutes, candidate_ids,
            profile_name=f'coverage_alpha={alpha:.2f}', weights=None,
        )
        points.append(ParetoPoint(
            alpha=alpha, efficiency_metric=solution.covered_ratio,
            equity_metric=solution.vulnerable_covered_ratio, solution=solution,
        ))
    return points


def non_dominated(points: list[ParetoPoint]) -> list[ParetoPoint]:
    """Keep only non-dominated solutions; showing dominated ones misleads
    the decision-maker."""
    keep = [p for p in points if not any(
        q.efficiency_metric >= p.efficiency_metric and q.equity_metric >= p.equity_metric
        and (q.efficiency_metric > p.efficiency_metric or q.equity_metric > p.equity_metric)
        for q in points)]
    unique = {}
    for point in keep:
        key = (round(point.efficiency_metric, 12), round(point.equity_metric, 12))
        unique.setdefault(key, point)
    return sorted(unique.values(), key=lambda p: p.efficiency_metric)
