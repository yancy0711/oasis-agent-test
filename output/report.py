"""Provenance-carrying decision report.

Principle: every conclusion states which data it rests on, which tier it came
from (live/cache/sample), and which value weights produced it. Numbers come
from the solver; the LLM may only polish the prose around them.
"""
from __future__ import annotations
from pathlib import Path
from agent.schemas import ParetoPoint, Provenance, ResultNarrative, SiteSolution, TaskSpec

_TEMPLATE = """# Siting Decision Report -- {place}

## 1. Task & Value Assumptions
- Intervention: {intervention}; max facilities: {n}; access threshold: {thr} min
- Value profile applied: {profile} (weights: {weights})
- Recorded assumptions/ambiguities: {notes}

## 2. Recommended Solution
- Selected sites: {sites}
- Total population coverage: {cov:.1%}
- Vulnerable population coverage: {vcov:.1%}
- Mean travel time: {tt:.1f} min | Solver status: {status}

## 3. Trade-off Analysis
{tradeoffs}

## 4. Data Provenance & Limitations
{lineage}

> Value weights in this report were set by a human decision-maker. The agent
> made no unilateral determination of "fairness" or "optimality".
"""


def render_report(task: TaskSpec, solution: SiteSolution,
                  frontier: list[ParetoPoint], lineage: list[Provenance]) -> str:
    """Render the deterministic decision report from typed results."""
    weights = solution.weights_used.model_dump() if solution.weights_used else 'coverage scalarization'
    return _TEMPLATE.format(
        place=task.place_name, intervention=task.intervention,
        n=task.n_facilities, thr=task.threshold_minutes,
        profile=solution.profile_name, weights=weights,
        notes=task.notes or "none",
        sites=", ".join(solution.selected_ids),
        cov=solution.covered_ratio, vcov=solution.vulnerable_covered_ratio,
        tt=solution.mean_travel_minutes, status=solution.solver_status,
        tradeoffs="\n".join(
            f"- alpha={p.alpha:.2f}: total {p.efficiency_metric:.1%}, "
            f"vulnerable {p.equity_metric:.1%}" for p in frontier),
        lineage="\n".join(
            f"- {p.source} ({p.tier} tier, {p.record_count} records, {p.crs}) {p.notes}"
            for p in lineage))

def append_agent_narrative(report_path: str, narrative: ResultNarrative) -> str:
    """Append the two evidence-grounded LLM paragraphs to a completed report."""
    section = ("\n\n## 5. Decision-Maker Interpretation\n\n" + narrative.decision_summary.strip() +
               "\n\n## 6. Agent Self-Reflection\n\n" + narrative.self_reflection.strip() + "\n")
    path = Path(report_path)
    path.write_text(path.read_text(encoding="utf-8").rstrip() + section, encoding="utf-8")
    return str(path)
