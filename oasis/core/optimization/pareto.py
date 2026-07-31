"""Parallel Pareto trade-off sweep for independent MCLP objectives."""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass
from multiprocessing import get_context
import os
import subprocess
from tempfile import TemporaryDirectory
from typing import Callable, Mapping

import numpy as np

from oasis.contracts.domain import ParetoPoint
from oasis.core.cancellation import RunCancelled
from oasis.core.optimization.models import (
    _site_solution_from_selection,
    _solve_mclp_selection,
)


_WORKER_COVER: np.ndarray | None = None
_WORKER_EFFICIENCY: np.ndarray | None = None
_WORKER_EQUITY: np.ndarray | None = None
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
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            process.terminate()
    executor.shutdown(wait=False, cancel_futures=True)
    for process in processes:
        process.join(timeout=1)

@dataclass(frozen=True)
class _ParetoResult:
    """Small process-safe result returned by one CBC solve."""

    index: int
    alpha: float
    chosen: list[int]
    solver_status: str


def _init_pareto_worker(
    cover_path: str,
    efficiency: np.ndarray,
    equity: np.ndarray,
    n_facilities: int,
    time_limit_s: int,
) -> None:
    """Memory-map shared coverage once in each spawned worker."""
    global _WORKER_COVER, _WORKER_EFFICIENCY, _WORKER_EQUITY
    global _WORKER_FACILITIES, _WORKER_TIME_LIMIT_S
    _WORKER_COVER = np.load(cover_path, mmap_mode="r", allow_pickle=False)
    _WORKER_EFFICIENCY = efficiency
    _WORKER_EQUITY = equity
    _WORKER_FACILITIES = n_facilities
    _WORKER_TIME_LIMIT_S = time_limit_s


def _solve_pareto_alpha(index: int, alpha: float) -> _ParetoResult:
    """Solve one independent Pareto objective inside a process worker."""
    if _WORKER_COVER is None or _WORKER_EFFICIENCY is None or _WORKER_EQUITY is None:
        raise RuntimeError("Pareto worker was not initialized")
    demand_weights = (
        (1 - alpha) * _WORKER_EFFICIENCY + alpha * _WORKER_EQUITY
    )
    chosen, status = _solve_mclp_selection(
        _WORKER_COVER,
        demand_weights,
        _WORKER_FACILITIES,
        _WORKER_TIME_LIMIT_S,
    )
    return _ParetoResult(index, alpha, chosen, status)


def _point_from_result(
    result: _ParetoResult,
    travel_matrix: np.ndarray,
    cover: np.ndarray,
    population: np.ndarray,
    vulnerable_mask: np.ndarray,
    candidate_ids: list[str],
) -> ParetoPoint:
    """Build metrics in the parent so workers never receive the travel matrix."""
    solution = _site_solution_from_selection(
        travel_matrix,
        cover,
        population,
        vulnerable_mask,
        candidate_ids,
        result.chosen,
        result.solver_status,
        profile_name=f"coverage_alpha={result.alpha:.2f}",
        weights=None,
    )
    return ParetoPoint(
        alpha=result.alpha,
        efficiency_metric=solution.covered_ratio,
        equity_metric=solution.vulnerable_covered_ratio,
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
) -> list[ParetoPoint]:
    """Sweep independent efficiency/equity objectives with bounded processes."""
    travel_matrix = np.asarray(travel_matrix, dtype=float)
    population = np.asarray(population, dtype=float)
    vulnerable_mask = np.asarray(vulnerable_mask, dtype=bool)
    if travel_matrix.ndim != 2:
        raise ValueError("travel_matrix must be two-dimensional")
    if population.ndim != 1 or vulnerable_mask.shape != population.shape:
        raise ValueError("population and vulnerable_mask must be aligned vectors")
    if travel_matrix.shape[0] != len(population):
        raise ValueError("travel_matrix rows must match population")
    if travel_matrix.shape[1] != len(candidate_ids):
        raise ValueError("candidate_ids must match travel_matrix columns")
    if not np.isfinite(population).all() or (population < 0).any() or population.sum() <= 0:
        raise ValueError("population must be non-negative, finite, and have positive total")
    if np.isnan(travel_matrix).any() or (travel_matrix < 0).any():
        raise ValueError("travel_matrix contains NaN or negative values")
    vulnerable_population = population * vulnerable_mask
    if vulnerable_population.sum() <= 0:
        raise ValueError("vulnerable_mask must contain positive-population demand")
    if threshold_minutes <= 0:
        raise ValueError("threshold_minutes must be positive")
    if steps < 2:
        raise ValueError("steps must be at least 2")
    if max_workers < 1:
        raise ValueError("max_workers must be positive")
    if time_limit_s <= 0:
        raise ValueError("time_limit_s must be positive")

    alphas = [float(value) for value in np.linspace(0, 1, steps)]
    points = dict(completed_points or {})
    for index, point in points.items():
        if index not in range(steps) or not np.isclose(point.alpha, alphas[index]):
            raise ValueError("completed_points do not match this Pareto sweep")
    if points and progress_callback:
        progress_callback("Pareto sweep", len(points), steps)
    if cancel_check:
        cancel_check()

    efficiency = population / population.sum()
    equity = vulnerable_population / vulnerable_population.sum()
    cover = travel_matrix <= threshold_minutes
    remaining = [
        (index, alpha) for index, alpha in enumerate(alphas) if index not in points
    ]

    def store(result: _ParetoResult) -> None:
        point = _point_from_result(
            result,
            travel_matrix,
            cover,
            population,
            vulnerable_mask,
            candidate_ids,
        )
        points[result.index] = point
        if checkpoint_callback:
            checkpoint_callback(result.index, point)
        if progress_callback:
            progress_callback("Pareto sweep", len(points), steps)

    if max_workers == 1:
        for index, alpha in remaining:
            if cancel_check:
                cancel_check()
            weights = (1 - alpha) * efficiency + alpha * equity
            chosen, status = _solve_mclp_selection(
                cover,
                weights,
                n_facilities,
                time_limit_s,
            )
            store(_ParetoResult(index, alpha, chosen, status))
        return [points[index] for index in range(steps)]

    if not remaining:
        return [points[index] for index in range(steps)]

    worker_count = min(max_workers, len(remaining))
    with TemporaryDirectory(prefix="oasis_pareto_") as directory:
        cover_path = f"{directory}/cover.npy"
        np.save(cover_path, cover, allow_pickle=False)
        executor = ProcessPoolExecutor(
            max_workers=worker_count,
            mp_context=get_context("spawn"),
            initializer=_init_pareto_worker,
            initargs=(
                cover_path,
                efficiency,
                equity,
                n_facilities,
                time_limit_s,
            ),
        )
        terminated = False
        jobs = iter(remaining)
        pending = {}

        def submit_next() -> bool:
            try:
                index, alpha = next(jobs)
            except StopIteration:
                return False
            pending[executor.submit(_solve_pareto_alpha, index, alpha)] = (
                index,
                alpha,
            )
            return True

        for _ in range(worker_count):
            submit_next()
        try:
            while pending:
                done, _ = wait(
                    tuple(pending), timeout=0.25, return_when=FIRST_COMPLETED
                )
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

    return [points[index] for index in range(steps)]


def non_dominated(points: list[ParetoPoint]) -> list[ParetoPoint]:
    """Keep unique solutions that are not dominated on both metrics."""
    keep = [p for p in points if not any(
        q.efficiency_metric >= p.efficiency_metric and q.equity_metric >= p.equity_metric
        and (q.efficiency_metric > p.efficiency_metric or q.equity_metric > p.equity_metric)
        for q in points)]
    unique = {}
    for point in keep:
        key = (round(point.efficiency_metric, 12), round(point.equity_metric, 12))
        unique.setdefault(key, point)
    return sorted(unique.values(), key=lambda p: p.efficiency_metric)
