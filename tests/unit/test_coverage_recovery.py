"""Recovery uses the primary constraints and changes exactly one editable value."""
import numpy as np
import pandas as pd
import pytest

from oasis.agent.context import Context
from oasis.agent.orchestrator import Orchestrator
from oasis.agent.recovery import coverage_recovery
from oasis.contracts.domain import TaskSpec, ToolResult, ValueWeights
from oasis.core.optimization.models import solve_mclp
from oasis.core.outputs.report import render_diagnostic_report
from oasis.services.workflow import find_minimum_facilities, find_minimum_threshold


def context(matrix, population, budget=1, threshold=15, target=1, capacity=None):
    ctx = Context()
    ctx.put("config", {"optimization": {"solver_time_limit_s": 5, "minimum_threshold_total_time_limit_s": 5}})
    ctx.put("task", TaskSpec(place_name="Test", n_facilities=budget, threshold_minutes=threshold,
                             coverage_target=target, facility_capacity=capacity, weights=ValueWeights()))
    population = np.asarray(population, float)
    ctx.put("demand", {"population": population, "vulnerable_mask": np.zeros(len(population), bool)})
    ctx.put("candidates", pd.DataFrame({"cand_id": [str(j) for j in range(len(matrix[0]))]}))
    ctx.put("travel", {"matrix": np.asarray(matrix, float)})
    return ctx


def test_two_independent_repairs_preserve_target_and_recommend_relative_change():
    ctx = context([[1, 20], [20, 1]], [100, 100])
    facilities = find_minimum_facilities(ctx, threshold_minutes=15).summary
    threshold = find_minimum_threshold(ctx, n_facilities=1).summary
    assert facilities["minimum_facilities"] == 2 and facilities["verified_for_siting"]
    assert threshold["minimum_threshold_minutes"] == 20.1 and threshold["verified_for_siting"]
    task = ctx.get("task")
    recovery = coverage_recovery(task, facilities, threshold, "Infeasible", "approximate")
    assert recovery.suggested_changes == {"threshold_minutes": 20.1}  # +34% versus +100%.
    assert len(recovery.alternatives) == 2
    report = render_diagnostic_report(recovery.model_dump(mode="json"), {})
    assert "Keep 15 minutes" in report and "Keep 1 facility" in report
    for option in recovery.alternatives:
        assert option["status"] == "verified"
        assert len(option["changes"]) == 1 and "coverage_target" not in option["changes"]
        fixed = {**task.model_dump(), **option["changes"]}
        solution = solve_mclp(ctx.get("travel")["matrix"], np.ones(2), np.array([100.,100.]), np.zeros(2, bool),
                              fixed["n_facilities"], fixed["threshold_minutes"], ["0", "1"], "check", None,
                              5, coverage_target=1)
        assert solution.solver_status in {"Optimal", "Feasible"} and solution.covered_ratio == pytest.approx(1)


def test_partial_population_target_does_not_become_a_tract_count_target():
    ctx = context([[1, 20], [20, 1]], [90, 10], target=.9, threshold=5)
    result = find_minimum_facilities(ctx, threshold_minutes=5, coverage_target=.9)
    assert result.summary["minimum_facilities"] == 1
    orch = Orchestrator(None, {})
    orch.ctx = ctx
    assert orch._quality_issue(None, ToolResult(ok=True, tool="optimize_sites", summary={"covered_tract_ratio": .5})) is None


def test_capacity_is_part_of_verified_count_and_time_alternatives():
    ctx = context([[1, 2], [2, 1]], [100, 100], threshold=5, capacity=100)
    assert find_minimum_facilities(ctx, threshold_minutes=5).summary["minimum_facilities"] == 2
    threshold = find_minimum_threshold(ctx, n_facilities=1).summary
    assert not threshold["feasible"]  # More time cannot fix an insufficient total capacity.


def test_exact_boundary_and_stale_diagnostics_are_not_applied():
    ctx = context([[102.61293069187015]], [100], threshold=12)
    count = find_minimum_facilities(ctx, threshold_minutes=12).summary
    threshold = find_minimum_threshold(ctx, n_facilities=1).summary
    assert threshold["minimum_threshold_minutes"] == 102.7
    task = ctx.get("task")
    recovery = coverage_recovery(task, count, threshold, "Infeasible", "approximate")
    assert recovery.alternatives[0]["status"] == "infeasible"
    assert recovery.suggested_changes == {"threshold_minutes": 102.7}
    stale = {**count, "feasible": True, "minimum_facilities": 10, "threshold_minutes": 102.7, "verified_for_siting": True}
    guarded = coverage_recovery(task, stale, {}, "Infeasible", "approximate")
    assert not guarded.suggested_changes and all(o["status"] != "verified" for o in guarded.alternatives)


def test_sparse_network_scope_does_not_offer_uncomputed_longer_trips():
    ctx = context([[np.inf]], [100])
    ctx.get("travel")["routing_report"] = {"router": "ors_sparse", "policy_minutes": 15,
                                             "mode_reports": {"walking": {"pruned_pairs": 1}}}
    result = find_minimum_threshold(ctx, n_facilities=1)
    assert not result.summary["feasible"] and result.summary["search"]["status"] == "routing_scope_limited"


def test_model_recovery_never_edits_coverage():
    task = TaskSpec(coverage_target=1)
    assert Orchestrator(None, {})._validated_changes(task, {"coverage_target": .9, "threshold_minutes": 20}) == {"threshold_minutes": 20}


@pytest.mark.parametrize("quote,scope,fraction,expected", [
    ("which could be reachable in the whole area", "full", 1, 1),
    ("cover 99% of residents", "partial", .99, .99),
    ("cover 90% area in riverside", "full", .9, .9),
])
def test_semantic_coverage_evidence_overrides_a_missed_or_rounded_target(quote, scope, fraction, expected):
    class LLM:
        def structured(self, system, user, schema):
            assert "coverage_intent" in schema["required"]
            return {"place_name": "Riverside", "analysis_goal": "facility_siting", "n_facilities": 10,
                    "threshold_minutes": 12, "coverage_target": .9 if scope == "partial" else None,
                    "coverage_intent": {"scope": scope, "evidence": quote, "fraction": fraction}}
    prompt = f"Select 10 facilities in Riverside within a 12-minute walking threshold, {quote}."
    task = Orchestrator(LLM(), {}).parse_task(prompt)
    assert task.analysis_goal == "facility_siting" and task.coverage_target == expected
    assert quote in task.notes


@pytest.mark.parametrize("prompt", [
    "Select 10 facilities in Riverside within 12 minutes; do not require whole-area coverage.",
    "Analyze heat across the whole study area in Riverside.",
])
def test_geographic_extent_and_negated_guarantee_do_not_create_a_coverage_target(prompt):
    class LLM:
        def structured(self, system, user, schema):
            return {"place_name": "Riverside", "analysis_goal": "facility_siting", "coverage_target": 1,
                    "coverage_intent": {"scope": "none", "evidence": "", "fraction": None}}
    assert Orchestrator(LLM(), {}).parse_task(prompt).coverage_target is None
