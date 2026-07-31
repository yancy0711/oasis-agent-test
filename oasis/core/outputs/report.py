"""Provenance-carrying decision report.

Principle: every conclusion states which data it rests on, which tier it came
from (live/cache/sample), and which value weights produced it. Numbers come
from the solver; the LLM may only polish the prose around them.
"""
from __future__ import annotations
from pathlib import Path
from oasis.contracts.domain import ParetoPoint, Provenance, ResultNarrative, SiteSolution, TaskSpec

_TEMPLATE = """# Siting Decision Report -- {place}

## 1. Task & Value Assumptions
- Intervention: {intervention}; max facilities: {n}; access threshold: {thr} min
- Mobility scenario: {mobility}; operating window: {operating_window}
- Availability semantics: operating hours restrict feasible supply before demand-weighted equity optimization; they are not a nearest-site recommendation.
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
        mobility={
            "walking": "Walking", "cycling": "Cycling", "driving": "Driving",
            "walk_drive_equity": (
                "Car-access equity: households without a car walk; others drive"
            ),
        }.get(task.mobility_scenario, task.mobility_scenario),
        operating_window=(task.operating_window.model_dump(mode="json")
                          if task.operating_window else "not applied"),
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

def render_diagnostic_report(recovery: dict | None, diagnostics: dict) -> str:
    """Render a concise report for a completed or interrupted feasibility diagnostic."""
    lines = ["# Feasibility Diagnostic", ""]
    if recovery: lines += [recovery["diagnosis"], "", f"Next decision: {recovery['question']}"]
    else: lines.append("The requested diagnostic completed successfully.")
    facilities = diagnostics.get("minimum_facilities", {})
    if facilities:
        value = facilities.get("minimum_facilities")
        lines += ["", "## Fixed-threshold lower bound",
                  f"- Feasible: {facilities.get('feasible', False)}",
                  f"- Minimum facilities: {value if value is not None else 'not achievable with current candidates'}",
                  f"- Threshold: {facilities.get('threshold_minutes', 'unknown')} minutes"]
    threshold = diagnostics.get("minimum_threshold", {})
    if threshold:
        value = threshold.get("minimum_threshold_minutes")
        display = f"{value:.1f} minutes" if value is not None else "not achievable with the current travel matrix"
        lines += ["", "## Fixed-budget lower bound",
                  f"- Feasible: {threshold.get('feasible', False)}",
                  f"- Minimum threshold: {display}",
                  f"- Facilities: {threshold.get('n_facilities', 'unknown')}"]
    return "\n".join(lines)

def render_heat_report(task: TaskSpec, summary: dict, lineage: list[Provenance]) -> str:
    """Render a compact deterministic report for a tract heat review."""
    stats = summary["statistics"]; moran = stats.get("morans_i"); p_value = stats.get("permutation_p_value")
    spatial = ("Global Moran's I could not be estimated because the retained tracts lack usable adjacency or temperature variation."
               if moran is None else
               f"Global Moran's I is {moran:.3f} with a two-sided permutation p-value of {p_value:.3f} "
               f"({stats['permutations']} permutations; {stats['islands']} tract islands).")
    hottest = "\n".join(f"- Tract {row['GEOID']}: {row['heat_exposure']:.1f} C" for row in summary["top_tracts"])
    sources = "\n".join(f"- {item.source} ({item.tier} tier, {item.record_count} records) {item.notes}" for item in lineage)
    return (f"# Heat Exposure Review -- {task.place_name}\n\n"
            f"## Deterministic findings\n\n"
            f"- Tracts reviewed: {stats['tracts']}\n"
            f"- Mean temperature: {stats['mean_c']:.1f} C; range: {stats['minimum_c']:.1f}-{stats['maximum_c']:.1f} C\n"
            f"- {spatial}\n\n## Highest-exposure tracts\n\n{hottest}\n\n"
            f"## Data provenance and interpretation limits\n\n{sources}\n\n"
            "> Temperatures are Landsat-derived land-surface temperature aggregated to Census tracts. "
            "Moran's I measures spatial clustering, not causation; its p-value is based on a fixed-seed permutation test.\n")


def append_heat_narrative(report_path: str, narrative: ResultNarrative) -> str:
    """Append one concise evidence-grounded LLM interpretation to a heat report."""
    paragraph = " ".join(part.strip() for part in (narrative.decision_summary, narrative.self_reflection) if part.strip())
    path = Path(report_path)
    path.write_text(path.read_text(encoding="utf-8").rstrip() + "\n\n## Agent interpretation\n\n" + paragraph + "\n", encoding="utf-8")
    return str(path)
