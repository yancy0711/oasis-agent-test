"""Deterministic efficiency--Gini trade-off search for gradual access."""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass
from multiprocessing import get_context
from math import ceil
import os
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Callable, Mapping

import numpy as np

from oasis.contracts.domain import ParetoPoint
from oasis.core.cancellation import RunCancelled
from oasis.core.optimization.models import (
    _site_solution_from_selection,
    _solve_gradual_assignment,
    gradual_coverage_matrix,
    population_weighted_gini,
)


_WORKER_BENEFIT: np.ndarray | None = None
_WORKER_POPULATION: np.ndarray | None = None
_WORKER_CAPACITIES: np.ndarray | None = None
_WORKER_FACILITIES = 0
_WORKER_TIME_LIMIT_S = 120


def _terminate_executor(executor: ProcessPoolExecutor) -> None:
    """Stop isolated solver workers and their CBC children without touching caches."""
    processes = list((getattr(executor, "_processes", None) or {}).values())
    for process in processes:
        if not process.is_alive():
            continue
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        else:
            process.terminate()
    executor.shutdown(wait=False, cancel_futures=True)
    for process in processes:
        process.join(timeout=1)


@dataclass(frozen=True)
class _ParetoResult:
    index: int
    alpha: float
    chosen: list[int]
    solver_status: str
    assignment: np.ndarray


def _gini_aware_solve(
    index: int,
    alpha: float,
    benefit: np.ndarray,
    population: np.ndarray,
    n_facilities: int,
    time_limit_s: int,
    capacities: np.ndarray | None,
) -> _ParetoResult:
    """Iteratively compensate underserved population and retain the true-Gini best."""
    efficiency_weights = population / population.sum()
    weights = efficiency_weights.copy()
    best: tuple[float, float, list[int], str, np.ndarray] | None = None
    rounds = 1 if alpha == 0 else 5
    deadline = monotonic() + time_limit_s
    for _ in range(rounds):
        remaining_seconds = max(1, ceil(deadline - monotonic()))
        chosen, status, assignment = _solve_gradual_assignment(
            benefit, weights, population, n_facilities, remaining_seconds,
            facility_capacities=capacities,
        )
        scores = np.sum(benefit * assignment, axis=1)
        efficiency = float(np.dot(population, scores) / population.sum())
        gini = population_weighted_gini(scores, population)
        equality = 0.0 if gini is None else 1.0 - gini
        objective = (1.0 - alpha) * efficiency + alpha * equality
        candidate = (objective, efficiency, chosen, status, assignment)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
        underserved = population * np.clip(1.0 - scores, 0.0, 1.0)
        if underserved.sum() <= 0:
            break
        underserved /= underserved.sum()
        weights = (1.0 - alpha) * efficiency_weights + alpha * underserved
        if monotonic() >= deadline:
            break
    assert best is not None
    return _ParetoResult(index, alpha, best[2], best[3], best[4])


def _init_pareto_worker(
    benefit_path: str,
    population: np.ndarray,
    capacities: np.ndarray | None,
    n_facilities: int,
    time_limit_s: int,
) -> None:
    global _WORKER_BENEFIT, _WORKER_POPULATION, _WORKER_CAPACITIES
    global _WORKER_FACILITIES, _WORKER_TIME_LIMIT_S
    _WORKER_BENEFIT = np.load(benefit_path, mmap_mode="r", allow_pickle=False)
    _WORKER_POPULATION = population
    _WORKER_CAPACITIES = capacities
    _WORKER_FACILITIES = n_facilities
    _WORKER_TIME_LIMIT_S = time_limit_s


def _solve_pareto_alpha(index: int, alpha: float) -> _ParetoResult:
    if _WORKER_BENEFIT is None or _WORKER_POPULATION is None:
        raise RuntimeError("Pareto worker was not initialized")
    return _gini_aware_solve(
        index, alpha, _WORKER_BENEFIT, _WORKER_POPULATION,
        _WORKER_FACILITIES, _WORKER_TIME_LIMIT_S, _WORKER_CAPACITIES,
    )


def _point_from_result(
    result: _ParetoResult,
    travel_matrix: np.ndarray,
    population: np.ndarray,
    vulnerable_mask: np.ndarray,
    candidate_ids: list[str],
    threshold_minutes: float,
    capacities: np.ndarray | None,
) -> ParetoPoint:
    solution = _site_solution_from_selection(
        travel_matrix, travel_matrix <= threshold_minutes, population,
        vulnerable_mask, candidate_ids, result.chosen, result.solver_status,
        profile_name=f"efficiency_gini_alpha={result.alpha:.2f}",
        weights=None, threshold_minutes=threshold_minutes,
        assignment=result.assignment, facility_capacities=capacities,
    )
    gini = solution.population_weighted_access_gini
    return ParetoPoint(
        alpha=result.alpha,
        efficiency_metric=solution.gradual_coverage_ratio,
        equity_metric=0.0 if gini is None else 1.0 - gini,
        population_weighted_gini=gini,
        solution=solution,
    )


def coverage_pareto_sweep(
    travel_matrix,
    population,
    vulnerable_mask,
    candidate_ids,
    n_facilities: int,
    threshold_minutes: float,
    steps: int = 11,
    progress_callback=None,
    *,
    max_workers: int = 1,
    time_limit_s: int = 120,
    cancel_check: Callable[[], None] | None = None,
    checkpoint_callback: Callable[[int, ParetoPoint], None] | None = None,
    completed_points: Mapping[int, ParetoPoint] | None = None,
    facility_capacities: np.ndarray | None = None,
    total_time_limit_s: int | None = None,
) -> list[ParetoPoint]:
    """Search a gradual-access efficiency versus actual weighted-Gini frontier."""
    travel_matrix = np.asarray(travel_matrix, dtype=float)
    population = np.asarray(population, dtype=float)
    vulnerable = np.asarray(vulnerable_mask)
    if travel_matrix.ndim != 2 or travel_matrix.shape[0] != len(population):
        raise ValueError("travel_matrix rows must match population")
    if population.ndim != 1 or vulnerable.shape != population.shape:
        raise ValueError("population and vulnerable_mask must be aligned vectors")
    if travel_matrix.shape[1] != len(candidate_ids):
        raise ValueError("candidate_ids must match travel_matrix columns")
    if not np.isfinite(population).all() or (population < 0).any() or population.sum() <= 0:
        raise ValueError("population must be non-negative, finite, and have positive total")
    if np.isnan(travel_matrix).any() or (travel_matrix < 0).any():
        raise ValueError("travel_matrix contains NaN or negative values")
    if (threshold_minutes <= 0 or steps < 2 or max_workers < 1 or time_limit_s <= 0
            or (total_time_limit_s is not None and total_time_limit_s <= 0)):
        raise ValueError("threshold, steps, workers, and time limit must be positive")
    capacities = None if facility_capacities is None else np.asarray(facility_capacities, float)
    if capacities is not None and (
        capacities.shape != (travel_matrix.shape[1],)
        or not np.isfinite(capacities).all() or (capacities <= 0).any()
    ):
        raise ValueError("facility capacities must be positive and aligned to candidates")

    alphas = [float(value) for value in np.linspace(0, 1, steps)]
    points = dict(completed_points or {})
    for index, point in points.items():
        if index not in range(steps) or not np.isclose(point.alpha, alphas[index]):
            raise ValueError("completed_points do not match this Pareto sweep")
    if points and progress_callback:
        progress_callback("Efficiency--Gini sweep", len(points), steps)
    if cancel_check:
        cancel_check()

    benefit = gradual_coverage_matrix(travel_matrix, threshold_minutes)
    remaining = [(i, alpha) for i, alpha in enumerate(alphas) if i not in points]
    deadline = (
        monotonic() + total_time_limit_s
        if total_time_limit_s is not None else None
    )

    def store(result: _ParetoResult) -> None:
        point = _point_from_result(
            result, travel_matrix, population, vulnerable, candidate_ids,
            threshold_minutes, capacities,
        )
        points[result.index] = point
        if checkpoint_callback:
            checkpoint_callback(result.index, point)
        if progress_callback:
            progress_callback("Efficiency--Gini sweep", len(points), steps)

    if max_workers == 1:
        for index, alpha in remaining:
            if cancel_check:
                cancel_check()
            if deadline is not None and monotonic() >= deadline:
                break
            store(_gini_aware_solve(
                index, alpha, benefit, population, n_facilities,
                min(time_limit_s, max(1, ceil(deadline - monotonic())))
                if deadline is not None else time_limit_s,
                capacities,
            ))
        return [points[index] for index in sorted(points)]
    if not remaining:
        return [points[index] for index in range(steps)]

    # Publish the efficiency baseline before starting slower equality-heavy solves.
    baseline = next((item for item in remaining if item[0] == 0), None)
    if baseline is not None:
        remaining.remove(baseline)
        baseline_limit = (
            min(time_limit_s, max(1, ceil(deadline - monotonic())))
            if deadline is not None else time_limit_s
        )
        store(_gini_aware_solve(
            baseline[0], baseline[1], benefit, population, n_facilities,
            baseline_limit, capacities,
        ))
    if not remaining or (deadline is not None and monotonic() >= deadline):
        return [points[index] for index in sorted(points)]

    # Start equality-heavy settings first after the visible baseline is available.
    remaining.sort(key=lambda item: item[1], reverse=True)

    worker_count = min(max_workers, len(remaining))
    with TemporaryDirectory(prefix="oasis_pareto_") as directory:
        benefit_path = f"{directory}/benefit.npy"
        np.save(benefit_path, benefit, allow_pickle=False)
        executor = ProcessPoolExecutor(
            max_workers=worker_count, mp_context=get_context("spawn"),
            initializer=_init_pareto_worker,
            initargs=(benefit_path, population, capacities, n_facilities, time_limit_s),
        )
        terminated = False
        jobs = iter(remaining)
        pending = {}

        def submit_next() -> bool:
            try:
                index, alpha = next(jobs)
            except StopIteration:
                return False
            pending[executor.submit(_solve_pareto_alpha, index, alpha)] = (index, alpha)
            return True

        for _ in range(worker_count):
            submit_next()
        try:
            while pending:
                if deadline is not None and monotonic() >= deadline:
                    for future in pending:
                        future.cancel()
                    _terminate_executor(executor)
                    terminated = True
                    break
                done, _ = wait(tuple(pending), timeout=.25, return_when=FIRST_COMPLETED)
                if not done:
                    if cancel_check:
                        cancel_check()
                    continue
                for future in done:
                    pending.pop(future)
                    store(future.result())
                    if cancel_check:
                        cancel_check()
                    submit_next()
        except RunCancelled:
            for future in pending:
                future.cancel()
            _terminate_executor(executor)
            terminated = True
            raise
        finally:
            if not terminated:
                executor.shutdown(wait=True)
    return [points[index] for index in sorted(points)]


def non_dominated(points: list[ParetoPoint]) -> list[ParetoPoint]:
    """Keep unique solutions that maximize gradual access and minimize Gini."""
    valid = [point for point in points if point.population_weighted_gini is not None]
    keep = [
        point for point in valid if not any(
            other.efficiency_metric >= point.efficiency_metric
            and other.population_weighted_gini <= point.population_weighted_gini
            and (
                other.efficiency_metric > point.efficiency_metric
                or other.population_weighted_gini < point.population_weighted_gini
            )
            for other in valid
        )
    ]
    unique = {}
    for point in keep:
        key = (
            round(point.efficiency_metric, 12),
            round(float(point.population_weighted_gini), 12),
        )
        unique.setdefault(key, point)
    return sorted(unique.values(), key=lambda point: point.efficiency_metric)
