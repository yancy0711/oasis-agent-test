"""Pareto frontier chart -- the paper's centerpiece figure."""
from __future__ import annotations
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from agent.schemas import ParetoPoint


def pareto_chart(points: list[ParetoPoint], frontier: list[ParetoPoint],
                 out_path: str) -> str:
    fig, ax = plt.subplots(figsize=(6, 5))
    ax.scatter([p.efficiency_metric for p in points],
               [p.equity_metric for p in points], alpha=.35, label="swept solutions")
    ax.plot([p.efficiency_metric for p in frontier],
            [p.equity_metric for p in frontier], "-o", label="non-dominated frontier")
    ax.set_xlabel("total population coverage")
    ax.set_ylabel("vulnerable population coverage")
    ax.set_title("Efficiency-Equity Trade-off Frontier")
    ax.legend(); ax.grid(alpha=.3)
    fig.tight_layout(); fig.savefig(out_path, dpi=150); plt.close(fig)
    return out_path
