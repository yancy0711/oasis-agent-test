"""Deterministic charts for siting trade-offs and heat-pattern analysis."""
from __future__ import annotations

import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter
import numpy as np

from oasis.contracts.domain import ParetoPoint


def pareto_chart(points: list[ParetoPoint], frontier: list[ParetoPoint], out_path: str) -> str:
    """Show total coverage against access inequality for the existing coverage sweep."""
    fig, ax = plt.subplots(figsize=(6, 5))
    gini = np.array([
        p.population_weighted_gini if p.population_weighted_gini is not None else np.nan
        for p in points
    ])
    alpha = np.array([p.alpha for p in points])
    valid = np.isfinite(gini)
    scatter = ax.scatter(
        np.array([p.efficiency_metric for p in points])[valid], gini[valid],
        c=alpha[valid], cmap="viridis", alpha=.8, zorder=3,
        label="efficiency--Gini solutions",
    )
    ordered = sorted((point for point in points if point.population_weighted_gini is not None), key=lambda point: point.alpha)
    if ordered:
        ax.plot(
            [p.efficiency_metric for p in ordered], [p.population_weighted_gini for p in ordered],
            color="#657b83", linewidth=1.2, alpha=.65, zorder=2, label="alpha sweep order",
        )
    frontier_ids = {id(point) for point in frontier}
    highlighted = [point for point in points if id(point) in frontier_ids and point.population_weighted_gini is not None]
    if highlighted:
        ax.scatter(
            [p.efficiency_metric for p in highlighted], [p.population_weighted_gini for p in highlighted],
            facecolors="none", edgecolors="#17211d", linewidths=1.4, s=70,
            zorder=4, label="coverage frontier points",
        )
    ax.set_xlabel("Population weighted gradual access (higher is better)")
    ax.xaxis.set_major_formatter(PercentFormatter(xmax=1.0, decimals=1))
    ax.set_ylabel("Population weighted access Gini (lower is more equal)")
    ax.set_title("Population Coverage vs Access Inequality")
    ax.legend(fontsize=8); ax.grid(alpha=.3)
    if valid.any():
        colorbar = fig.colorbar(scatter, ax=ax)
        colorbar.set_label("Gini emphasis alpha (higher prioritizes equality)")
    fig.tight_layout(); fig.savefig(out_path, dpi=150); plt.close(fig)
    return out_path


def _moran_statistics(tracts: gpd.GeoDataFrame, permutations: int = 199) -> tuple[dict, np.ndarray, np.ndarray]:
    """Calculate global Moran's I with queen adjacency and a permutation p-value."""
    values = np.asarray(tracts["heat_exposure"], dtype=float); valid = np.isfinite(values)
    frame, values = tracts.loc[valid].reset_index(drop=True), values[valid]
    mean, std = float(values.mean()), float(values.std())
    basic = {"tracts": len(values), "minimum_c": float(values.min()), "maximum_c": float(values.max()),
             "mean_c": mean, "median_c": float(np.median(values)), "standard_deviation_c": std}
    if len(values) < 2 or std == 0: return {**basic, "morans_i": None, "permutation_p_value": None,
                                            "permutations": permutations, "islands": len(values)}, np.zeros(len(values)), np.full(len(values), np.nan)
    pairs = [(i, int(j)) for i, geom in enumerate(frame.geometry)
             for j in frame.sindex.query(geom, predicate="touches") if int(j) != i]
    if not pairs: return {**basic, "morans_i": None, "permutation_p_value": None,
                          "permutations": permutations, "islands": len(values)}, (values - mean) / std, np.full(len(values), np.nan)
    rows, cols = np.asarray(pairs, dtype=int).T; z = (values - mean) / std
    denominator = float(np.dot(z, z)); observed = len(values) / len(rows) * float(np.sum(z[rows] * z[cols])) / denominator
    rng = np.random.default_rng(42)
    simulated = np.asarray([len(values) / len(rows) * float(np.sum(p[rows] * p[cols])) / denominator
                            for p in (rng.permutation(z) for _ in range(permutations))])
    p_value = float((np.count_nonzero(np.abs(simulated) >= abs(observed)) + 1) / (permutations + 1))
    counts = np.bincount(rows, minlength=len(values)); lag = np.full(len(values), np.nan)
    lag[counts > 0] = np.bincount(rows, weights=z[cols], minlength=len(values))[counts > 0] / counts[counts > 0]
    return {**basic, "morans_i": float(observed), "permutation_p_value": p_value,
            "permutations": permutations, "islands": int(np.count_nonzero(counts == 0))}, z, lag


def heat_analysis_chart(tracts: gpd.GeoDataFrame, out_path: str, permutations: int = 199) -> dict:
    """Render temperature distribution and Moran scatter plots in one row."""
    stats, z, lag = _moran_statistics(tracts, permutations)
    values = np.asarray(tracts["heat_exposure"], dtype=float); values = values[np.isfinite(values)]
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.2))
    axes[0].hist(values, bins=min(12, max(4, int(np.sqrt(len(values))))), color="#EF9F27", edgecolor="white")
    axes[0].axvline(stats["mean_c"], color="#185FA5", linestyle="--", label=f"Mean {stats['mean_c']:.1f} C")
    axes[0].set(title="Tract heat-exposure distribution", xlabel="Mean land-surface temperature (°C)", ylabel="Census tracts")
    axes[0].legend(frameon=False)
    finite = np.isfinite(lag)
    if stats["morans_i"] is None or not finite.any():
        axes[1].text(.5, .5, "No tract adjacency or temperature variation", ha="center", va="center", transform=axes[1].transAxes)
    else:
        axes[1].scatter(z[finite], lag[finite], color="#185FA5", alpha=.7, edgecolor="white")
        if finite.sum() > 1:
            x = np.linspace(float(z[finite].min()), float(z[finite].max()), 50)
            axes[1].plot(x, np.polyval(np.polyfit(z[finite], lag[finite], 1), x), color="#EF9F27")
        axes[1].axhline(0, color="#999", linewidth=.8); axes[1].axvline(0, color="#999", linewidth=.8)
        axes[1].set_title(f"Moran scatter: I={stats['morans_i']:.3f}, p={stats['permutation_p_value']:.3f}")
    axes[1].set(xlabel="Standardized tract temperature", ylabel="Mean standardized neighbor temperature")
    fig.tight_layout(); fig.savefig(out_path, dpi=160, bbox_inches="tight"); plt.close(fig)
    return stats
