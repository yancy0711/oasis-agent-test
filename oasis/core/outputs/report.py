"""Deterministic, provenance-carrying Markdown reports."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from oasis.contracts.domain import ParetoPoint, Provenance, ResultNarrative, SiteSolution, TaskSpec


def _method_section(
    task: TaskSpec,
    solution: SiteSolution,
    context: dict[str, Any],
) -> str:
    weights = solution.weights_used.normalized().model_dump() if solution.weights_used else None
    if weights:
        weight_text = ", ".join(f"{name}={value:.3f}" for name, value in weights.items())
        demand_formula = (
            "The requested value profile formed each tract's demand weight as:\n\n"
            "$$\n"
            r"w_i = P_i \left[\beta_{\mathrm{pop}}"
            r" + \beta_{\mathrm{old}}z(\mathrm{old}_i)"
            r" + \beta_{\mathrm{pov}}z(\mathrm{poverty}_i)"
            r" + \beta_{\mathrm{heat}}z(E_i)"
            r" + \beta_{\mathrm{env}}z(B_i)\right]"
            "\n$$\n\n"
            f"Applied normalized coefficients: `{weight_text}`. Here `z(.)` is min-max "
            "normalization over retained tracts. $B_i$ is the passive built-environment burden. The public `population_density` coefficient "
            "is retained for compatibility but acts as the unmodified population baseline; "
            "density is not multiplied twice."
        )
    else:
        demand_formula = (
            "This solution used the population-efficiency demand weight:\n\n"
            "$$\n"
            r"w_i = P_i"
            "\n$$\n\n"
            "No demographic value-profile coefficient altered the selected solution."
        )
    capacity_active = bool(solution.facility_capacities)
    capacity_text = (
        "Capacity constraints were active using observed/user-confirmed people capacities."
        if capacity_active else
        "Capacity constraints were inactive because complete absolute people capacities were not available. "
        "OSM footprint and floor-area fields remain relative diagnostics and were not converted into people."
    )
    environment = context.get("built_environment", {})
    environment_status = environment.get("status", "not run")
    environment_weight = weights.get("environment_burden", 0.0) if weights else 0.0
    vulnerability = context.get("vulnerability", {})
    sensitivity = vulnerability.get("weight_sensitivity", {})
    comparisons = sensitivity.get("comparisons", [])
    minimum_rank = min(
        (item.get("rank_correlation") for item in comparisons
         if item.get("rank_correlation") is not None),
        default=None,
    )
    minimum_jaccard = min(
        (item.get("priority_tract_jaccard") for item in comparisons
         if item.get("priority_tract_jaccard") is not None),
        default=None,
    )
    missingness = vulnerability.get("missingness", [])
    imputed = [item for item in missingness if item.get("missing_count", 0)]
    missing_text = (
        " Missing-value actions: " + "; ".join(
            f"{item['indicator']} had {item['missing_count']} missing tract(s) "
            f"({item.get('missing_percent', 0):.1%}) and used {item.get('strategy')} "
            f"with fill value {item.get('fill_value'):.3f}"
            for item in imputed
        ) + "."
        if imputed else " No configured vulnerability indicator required imputation."
    )
    within_pairs = vulnerability.get("correlation", {}).get("high_pairs", [])
    within_pair_text = (
        f" The configured warning threshold identified {len(within_pairs)} high-correlation pair(s); "
        + "; ".join(
            f"{item['indicators']} ($r_s={item['correlation']:.3f}$)"
            for item in within_pairs[:3]
        ) + "."
        if within_pairs else " No vulnerability-input pair exceeded the configured correlation warning threshold."
    )
    vifs = [item for item in vulnerability.get("vif", []) if item.get("vif") is not None]
    vif_text = (
        f" The maximum VIF was {max(vifs, key=lambda item: item['vif'])['vif']:.3f} "
        f"for `{max(vifs, key=lambda item: item['vif'])['indicator']}`."
        if vifs else ""
    )
    pca = vulnerability.get("pca", {})
    pca_ratios = pca.get("explained_variance_ratio", [])
    pca_text = (
        f" PC1 explained {pca_ratios[0]:.1%} of standardized indicator variance, and "
        f"{pca.get('components_for_80_percent')} component(s) reached 80%."
        if pca_ratios else ""
    )
    diagnostic_pairs = vulnerability.get("diagnostic_correlation", {}).get("strongest_pairs", [])
    diagnostic_text = (
        " The strongest pairwise-complete relationship across vulnerability, temperature, and built-environment diagnostics was "
        f"{diagnostic_pairs[0]['features']} ($r_s={diagnostic_pairs[0]['correlation']:.3f}$; "
        f"{diagnostic_pairs[0]['paired_tracts']} paired tracts). This is an association, not a causal effect."
        if diagnostic_pairs else ""
    )
    vulnerability_text = (
        f"The active `{vulnerability.get('active_scheme')}` scheme used "
        f"`{vulnerability.get('normalization')}` normalization and defined priority as "
        f"{vulnerability.get('priority_definition')}. The run audited missingness, "
        "indicator direction, Spearman correlation, VIF, PCA, and alternative weighting schemes. "
        + (f"Across tested schemes, the minimum rank correlation was {minimum_rank:.3f} "
           f"and minimum priority-tract Jaccard overlap was {minimum_jaccard:.3f}. "
           if minimum_rank is not None and minimum_jaccard is not None else "")
        + missing_text + within_pair_text + vif_text + pca_text + diagnostic_text
        + " PCA loadings are a covariance diagnostic, not evidence of causal or normative importance."
        if vulnerability else
        "No vulnerability preprocessing diagnostic was available for this run."
    )
    return rf"""## 4. Model Equations and Variable Definitions

### 4.1 Population-weighted heat exposure

$$
E_i =
\frac{{\displaystyle\sum_{{g \in i}} P_g\,\mathrm{{LST}}_g}}
{{\displaystyle\sum_{{g \in i}} P_g}}
$$

- $i$: Census tract; $g$: sub-tract population unit or calibrated grid cell.
- $P_g$: population assigned to cell $g$; $\mathrm{{LST}}_g$: clear-pixel Landsat land-surface temperature.
- Population weighting is accepted only when valid-LST cells contain at least {context.get('population_weighting_threshold', .8):.0%} of tract population; otherwise the explicitly labelled tract-mean fallback is used.

### 4.2 Demand weights

{demand_formula}

### 4.3 Gradual network coverage and assignment

$$
c_{{ij}} = \max\!\left(0, 1 - \frac{{d_{{ij}}}}{{T}}\right)
$$

$$
A_i = \sum_j c_{{ij}}q_{{ij}}
$$

$$
\max_{{x,q}} \quad \sum_i\sum_j w_i c_{{ij}}q_{{ij}}
$$

- $d_{{ij}}$: network travel time from demand tract $i$ to candidate facility $j$.
- $T$: confirmed travel-time threshold ({task.threshold_minutes:g} minutes).
- $c_{{ij}}$: gradual coverage, equal to 1 at zero minutes and 0 at or beyond $T$.
- $x_j \in \{{0,1\}}$: whether site $j$ is selected; $q_{{ij}} \in [0,1]$: share of tract $i$ assigned to site $j$.
- Selection and assignment constraints are $\sum_j x_j \le {task.n_facilities}$, $\sum_j q_{{ij}} \le 1$, and $q_{{ij}} \le x_j$.
- When capacity is active, $\sum_i P_i q_{{ij}} \le K_jx_j$, where $K_j$ is site $j$'s people capacity.
- {capacity_text}

### 4.4 Population-weighted access inequality

$$
G_P =
\frac{{\displaystyle\sum_i\sum_k P_iP_k\lvert A_i-A_k\rvert}}
{{\displaystyle 2\left(\sum_iP_i\right)\left(\sum_iP_iA_i\right)}}
$$

$$
\bar A_P = \frac{{\sum_iP_iA_i}}{{\sum_iP_i}},
\qquad
J_\alpha = (1-\alpha)\bar A_P + \alpha(1-G_P)
$$

- $A_i$: gradual access score after assignment; $G_P$: population-weighted access Gini.
- Lower $G_P$ is more equal. It is undefined when every access score is zero.
- The displayed frontier evaluates the actual Gini. Gini-aware solutions use deterministic iterative reweighting around exact CBC assignment solves, so they are reproducible feasible solutions rather than proof of a globally optimal nonlinear-Gini frontier.

### 4.5 Built-environment diagnostics

$$
\mathrm{{NDVI}} = \frac{{\mathrm{{NIR}}-\mathrm{{Red}}}}{{\mathrm{{NIR}}+\mathrm{{Red}}}},
\qquad
\mathrm{{NDBI}} = \frac{{\mathrm{{SWIR1}}-\mathrm{{NIR}}}}{{\mathrm{{SWIR1}}+\mathrm{{NIR}}}}
$$

$$
\mathrm{{Albedo}}_{{\mathrm{{proxy}}}} =
0.356\,\mathrm{{Blue}} + 0.130\,\mathrm{{Red}} + 0.373\,\mathrm{{NIR}}
+ 0.085\,\mathrm{{SWIR1}} + 0.072\,\mathrm{{SWIR2}} - 0.0018
$$

The passive burden is calculated as:

$$
B_i = \operatorname{{mean}}\left[F(\mathrm{{NDBI}}_i),\ 1-F(\mathrm{{NDVI}}_i),\ 1-F(\mathrm{{Albedo}}_i)\right]
$$

where $F$ is the within-run percentile transform over available tracts. Built-environment status: `{environment_status}`; confirmed coefficient $\beta_{{\mathrm{{env}}}}={environment_weight:.3f}$. Thus $B_i$ is included in $w_i$ whenever that coefficient is positive. It does not directly alter $c_{{ij}}$ or the access Gini. On-demand route heat remains a post-route audit: it will enter optimization only when real route geometry is available for every demand-site pair, rather than using tract-centroid lines.

### 4.6 Vulnerability preprocessing and robustness

{vulnerability_text}
"""


def render_report(
    task: TaskSpec,
    solution: SiteSolution,
    frontier: list[ParetoPoint],
    lineage: list[Provenance],
    model_context: dict[str, Any] | None = None,
) -> str:
    """Render the deterministic decision report from typed results."""
    weights = solution.weights_used.model_dump() if solution.weights_used else "population efficiency"
    mobility = {
        "walking": "Walking", "cycling": "Cycling", "driving": "Driving",
        "walk_drive_equity": "Car-access equity: households without a car walk; others drive",
    }.get(task.mobility_scenario, task.mobility_scenario)
    tradeoffs = "\n".join(
        f"- α = {point.alpha:.2f}: gradual access {point.efficiency_metric:.1%}, "
        f"equality {point.equity_metric:.1%}, population-weighted access Gini "
        f"{point.population_weighted_gini:.3f}"
        if point.population_weighted_gini is not None else
        f"- α = {point.alpha:.2f}: gradual access {point.efficiency_metric:.1%}, Gini undefined"
        for point in frontier
    )
    sources = "\n".join(
        f"- {item.source} ({item.tier} tier, {item.record_count} records, {item.crs}) {item.notes}"
        for item in lineage
    )
    method = _method_section(task, solution, model_context or {})
    capacity = (
        f"{task.facility_capacity:.0f} people per facility"
        if task.facility_capacity is not None else
        "observed candidate capacities" if solution.facility_capacities else "not constrained"
    )
    gini = (
        f"{solution.population_weighted_access_gini:.3f}"
        if solution.population_weighted_access_gini is not None else "undefined"
    )
    utilization = (
        f"{solution.maximum_capacity_utilization:.1%}"
        if solution.maximum_capacity_utilization is not None else "not applicable"
    )
    return f"""# Candidate Facility Selection Report — {task.place_name}

## 1. Task and Value Assumptions

- Intervention: {task.intervention}; maximum facilities: {task.n_facilities}; access threshold: {task.threshold_minutes:g} minutes.
- Mobility scenario: {mobility}; operating window: {task.operating_window.model_dump(mode='json') if task.operating_window else 'not applied'}.
- Coverage model: linear gradual network coverage with explicit splittable demand assignment.
- Facility capacity: {capacity}.
- Final-solution equity trade-off: {f'α = {task.equity_alpha:.1f} on the efficiency–Gini frontier' if task.equity_alpha is not None else 'not applied; the confirmed value profile selected the final solution'}.
- Value profile: {solution.profile_name}; weights: `{weights}`.
- Recorded assumptions or ambiguities: {task.notes or 'none'}.

## 2. Recommended Solution

- Selected candidate facilities: {', '.join(solution.selected_ids)}
- Assigned population share: {solution.covered_ratio:.1%}
- Composite-vulnerability priority population assigned share: {solution.vulnerable_covered_ratio:.1%}
- Population-weighted access Gini: {gini} (lower is more equal)
- Mean gradual access score: {solution.mean_access_score:.3f}
- Assigned population: {solution.assigned_population:.0f}; maximum capacity utilization: {utilization}
- Assignment-weighted mean travel time: {solution.mean_travel_minutes:.1f} minutes
- Solver status: {solution.solver_status}

## 3. Efficiency–Equity Trade-off

{tradeoffs}

{method}

## 5. Data Provenance and Limitations

{sources}

> OASIS selects from existing candidate facilities; it does not identify vacant land or confirm that an OpenStreetMap facility is currently operating as a cooling center. Human-confirmed weights and constraints define the normative choices.
"""


def append_agent_narrative(report_path: str, narrative: ResultNarrative) -> str:
    section = ("\n\n## 6. Decision-Maker Interpretation\n\n" + narrative.decision_summary.strip() +
               "\n\n## 7. Agent Self-Reflection\n\n" + narrative.self_reflection.strip() + "\n")
    path = Path(report_path)
    path.write_text(path.read_text(encoding="utf-8").rstrip() + section, encoding="utf-8")
    return str(path)


def render_diagnostic_report(recovery: dict | None, diagnostics: dict) -> str:
    lines = ["# Feasibility Diagnostic", ""]
    if recovery: lines += [recovery["diagnosis"], "", f"Next decision: {recovery['question']}"]
    else: lines.append("The requested diagnostic completed successfully.")
    answers = diagnostics.get("result_query_answers", {}).get("answers", [])
    if answers:
        lines.extend(["", "## Requested result queries", ""])
        for answer in answers:
            computation = answer.get("computation", {})
            lines.extend([f"### {answer.get('metric_label', 'Requested metric').title()}",
                          answer.get("method", ""),
                          f"Formula: `{computation.get('formula', 'recorded indicator ranking')}`",
                          f"Data basis: {computation.get('data_basis', '')}"])
            for row in answer.get("top_rows", []):
                lines.append(
                    f"- {row.get('rank')}. Tract {row.get('GEOID')}: "
                    f"{answer.get('metric_label', 'requested metric')} = {row.get('value'):.3f}"
                )
    facilities = diagnostics.get("minimum_facilities", {})
    if facilities:
        value = facilities.get("minimum_facilities")
        lines += ["", "## Fixed-threshold lower bound", f"- Feasible: {facilities.get('feasible', False)}",
                  f"- Minimum facilities: {value if value is not None else 'not achievable with current candidates'}",
                  f"- Threshold: {facilities.get('threshold_minutes', 'unknown')} minutes"]
    threshold = diagnostics.get("minimum_threshold", {})
    if threshold:
        value = threshold.get("minimum_threshold_minutes")
        display = f"{value:.1f} minutes" if value is not None else "not achievable with the current travel matrix"
        lines += ["", "## Fixed-budget lower bound", f"- Feasible: {threshold.get('feasible', False)}",
                  f"- Minimum threshold: {display}", f"- Facilities: {threshold.get('n_facilities', 'unknown')}"]
    approximate_threshold = diagnostics.get("approximate_minimum_threshold", {})
    if approximate_threshold:
        value = approximate_threshold.get("minimum_threshold_minutes")
        display = f"{value:.1f} minutes" if value is not None else "unavailable"
        lines += ["", "## Approximate complementary bound",
                  "- Accuracy: Haversine screening estimate; not an ORS network result",
                  f"- Estimated minimum threshold: {display}",
                  f"- Facilities: {approximate_threshold.get('n_facilities', 'unknown')}",
                  f"- Interpretation: {approximate_threshold.get('interpretation', '')}"]
    reachability = diagnostics.get("reachability_analysis", {})
    if reachability:
        lines += ["", "## Reachability review",
                  f"- Travel threshold: {reachability.get('threshold_minutes', 'unknown')} minutes",
                  f"- Maximum reachable tract share: {reachability.get('maximum_tract_coverage', 0):.1%}",
                  f"- Unserved demand units: {len(reachability.get('unserved_demand_ids', []))}",
                  "", "### Hardest demand units"]
        for row in reachability.get("hardest_demand", []):
            minutes = row.get("nearest_minutes")
            label = "unreachable" if minutes is None else f"{minutes:.1f} minutes"
            location = (
                f"; map location {row['latitude']:.5f}, {row['longitude']:.5f}"
                if row.get("latitude") is not None and row.get("longitude") is not None
                else ""
            )
            marker = "; highlighted on the interactive map" if row.get("rank") == 1 else ""
            lines.append(
                f"- {row.get('demand_id')}: {label}; "
                f"{row.get('reachable_candidates', 0)} candidates within threshold"
                f"{location}{marker}"
            )
    return "\n".join(lines)


def render_heat_report(task: TaskSpec, summary: dict, lineage: list[Provenance]) -> str:
    stats = summary["statistics"]; moran = stats.get("morans_i"); p_value = stats.get("permutation_p_value")
    spatial = ("Global Moran's I could not be estimated because the retained tracts lack usable adjacency or temperature variation."
               if moran is None else f"Global Moran's I is {moran:.3f} with a two-sided permutation p-value of {p_value:.3f} "
               f"({stats['permutations']} permutations; {stats['islands']} tract islands).")
    hottest = "\n".join(f"- Tract {row['GEOID']}: {row['heat_exposure']:.1f} °C" for row in summary["top_tracts"])
    sources = "\n".join(f"- {item.source} ({item.tier} tier, {item.record_count} records) {item.notes}" for item in lineage)
    return (f"# Heat Exposure Review — {task.place_name}\n\n## Deterministic findings\n\n"
            f"- Tracts reviewed: {stats['tracts']}\n- Mean temperature: {stats['mean_c']:.1f} °C; range: {stats['minimum_c']:.1f}–{stats['maximum_c']:.1f} °C\n"
            f"- {spatial}\n\n## Highest-exposure tracts\n\n{hottest}\n\n## Measurement basis\n\n{summary.get('evidence_basis', 'Landsat-derived tract heat exposure.')}\n\n## Data provenance and interpretation limits\n\n{sources}\n\n"
            "> Temperatures are Landsat-derived land-surface temperature aggregated to Census tracts. Moran's I measures spatial clustering, not causation.\n")


def append_heat_narrative(report_path: str, narrative: ResultNarrative) -> str:
    paragraph = " ".join(part.strip() for part in (narrative.decision_summary, narrative.self_reflection) if part.strip())
    path = Path(report_path)
    path.write_text(path.read_text(encoding="utf-8").rstrip() + "\n\n## Agent interpretation\n\n" + paragraph + "\n", encoding="utf-8")
    return str(path)
