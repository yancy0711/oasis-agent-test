"""Pure helpers for mobility scenarios and grouped optimization demand."""
from __future__ import annotations

from typing import Any

import numpy as np

SCENARIO_MODES = {
    "walking": ("walking",),
    "cycling": ("cycling",),
    "driving": ("driving",),
    "walk_drive_equity": ("walking", "driving"),
}


def scenario_modes(scenario: str) -> tuple[str, ...]:
    """Return the routing profiles required by one confirmed scenario."""
    try:
        return SCENARIO_MODES[scenario]
    except KeyError as exc:
        raise ValueError(f"Unsupported mobility scenario: {scenario}") from exc


def compose_demand(
    demand: dict[str, Any],
    matrices: dict[str, np.ndarray],
    scenario: str,
) -> dict[str, Any]:
    """Align matrices and demand weights for deterministic optimization."""
    modes = scenario_modes(scenario)
    missing = set(modes) - set(matrices)
    if missing:
        raise ValueError(f"Missing travel matrices for: {', '.join(sorted(missing))}")

    if scenario != "walk_drive_equity" and "population" not in demand:
        matrix = np.asarray(matrices[modes[0]], dtype=float)
        return {
            "matrix": matrix,
            "base_index": np.arange(matrix.shape[0]),
            "mode": np.full(matrix.shape[0], modes[0], dtype=object),
            "mode_population_share": {modes[0]: 1.0},
        }

    population = np.asarray(demand["population"], dtype=float)
    vulnerable = np.asarray(demand["vulnerable_mask"], dtype=bool)
    indicators = {
        name: np.asarray(values, dtype=float)
        for name, values in demand["indicators"].items()
    }
    expected = (len(population), next(iter(matrices.values())).shape[1])
    for mode in modes:
        if np.asarray(matrices[mode]).shape != expected:
            raise ValueError(f"{mode} matrix does not align with demand and candidates")

    if scenario != "walk_drive_equity":
        return {
            "matrix": np.asarray(matrices[modes[0]], dtype=float),
            "population": population,
            "vulnerable_mask": vulnerable,
            "indicators": indicators,
            "base_index": np.arange(len(population)),
            "mode": np.full(len(population), modes[0], dtype=object),
            "mode_population_share": {modes[0]: 1.0},
        }

    carless = np.asarray(demand["no_vehicle_ratio"], dtype=float)
    if carless.shape != population.shape or not np.isfinite(carless).all():
        raise ValueError("no_vehicle_ratio must align with demand and contain no missing values")
    carless = np.clip(carless, 0.0, 1.0)
    shares = {"walking": carless, "driving": 1.0 - carless}
    rows = []
    for mode in modes:
        active = population * shares[mode] > 0
        rows.append((mode, active))

    matrix = np.vstack([np.asarray(matrices[mode])[active] for mode, active in rows])
    grouped_population = np.concatenate(
        [population[active] * shares[mode][active] for mode, active in rows]
    )
    grouped_vulnerable = np.concatenate([vulnerable[active] for _, active in rows])
    grouped_indicators = {
        name: np.concatenate([values[active] for _, active in rows])
        for name, values in indicators.items()
    }
    base_index = np.concatenate(
        [np.flatnonzero(active) for _, active in rows]
    )
    mode_labels = np.concatenate(
        [np.full(int(active.sum()), mode, dtype=object) for mode, active in rows]
    )
    total = max(float(grouped_population.sum()), 1.0)
    mode_share = {
        mode: float(grouped_population[mode_labels == mode].sum() / total)
        for mode in modes
    }
    return {
        "matrix": matrix,
        "population": grouped_population,
        "vulnerable_mask": grouped_vulnerable,
        "indicators": grouped_indicators,
        "base_index": base_index,
        "mode": mode_labels,
        "mode_population_share": mode_share,
    }
