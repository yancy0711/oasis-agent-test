"""Unit tests for the deterministic core. No external API calls.

Purpose: (1) keep the solver verifiable; (2) show reviewers exactly which
behaviors are guaranteed vs model-decided.
"""
import numpy as np
from agent.schemas import ValueWeights
from tools.optimization.models import (build_demand_weights, minimum_coverage_threshold,
                                       solve_lscp, solve_mclp)
from tools.optimization.pareto import coverage_pareto_sweep, non_dominated


def test_mclp_prefers_high_weight_demand():
    # 3 demand points, 3 candidates, one-to-one coverable; budget = 1 facility
    tm = np.array([[5., 99., 99.], [99., 5., 99.], [99., 99., 5.]])
    pop = np.array([100., 100., 100.])
    ind = {"poverty_rate": np.array([0.1, 0.9, 0.1])}   # point 2 is most vulnerable
    w = ValueWeights(population_density=0., elderly_ratio=0., poverty_rate=1., heat_exposure=0.)
    sol = solve_mclp(tm, build_demand_weights(ind, pop, w), pop,
                     ind["poverty_rate"] > 0.5, 1, 15., ["a", "b", "c"], "test", w)
    assert sol.selected_ids == ["b"], "equity weighting should pick the site serving the vulnerable point"
    assert sol.solver_status == "Optimal"


def test_unreachable_demand_yields_zero_coverage_not_error():
    tm = np.full((2, 2), 99.)
    pop = np.array([50., 50.])
    w = ValueWeights()
    sol = solve_mclp(tm, build_demand_weights({"population_density": pop}, pop, w),
                     pop, pop > 1e9, 1, 15., ["a", "b"], "test", w)
    assert sol.covered_ratio == 0.0


def test_changing_weights_changes_selected_site():
    # Site a covers the populous tract. Site b covers two smaller but
    # elderly/poor tracts. The selected site must respond to value weights.
    tm = np.array([
        [5., 99.],
        [99., 5.],
        [99., 5.],
    ])
    pop = np.array([1000., 100., 100.])
    indicators = {
        "population_density": np.array([1., 0., 0.]),
        "elderly_ratio": np.array([0., 1., 0.]),
        "poverty_rate": np.array([0., 0., 1.]),
    }
    vulnerable = np.array([False, True, True])
    efficiency = ValueWeights(
        population_density=1., elderly_ratio=0., poverty_rate=0., heat_exposure=0.
    )
    equity = ValueWeights(
        population_density=0., elderly_ratio=0.5, poverty_rate=0.5, heat_exposure=0.
    )

    sol_eff = solve_mclp(
        tm, build_demand_weights(indicators, pop, efficiency), pop, vulnerable,
        1, 15., ["a", "b"], "efficiency", efficiency,
    )
    sol_eq = solve_mclp(
        tm, build_demand_weights(indicators, pop, equity), pop, vulnerable,
        1, 15., ["a", "b"], "equity", equity,
    )

    assert sol_eff.selected_ids == ["a"]
    assert sol_eq.selected_ids == ["b"]
    assert sol_eq.vulnerable_covered_ratio > sol_eff.vulnerable_covered_ratio


def test_coverage_is_monotonic_as_facility_budget_grows():
    tm = np.array([
        [5., 99., 99.],
        [99., 5., 99.],
        [99., 99., 5.],
    ])
    pop = np.array([100., 100., 100.])
    indicators = {"population_density": np.array([1., 2., 3.])}
    vulnerable = np.zeros(3, dtype=bool)
    weights = ValueWeights(
        population_density=1., elderly_ratio=0., poverty_rate=0., heat_exposure=0.
    )
    demand_weights = build_demand_weights(indicators, pop, weights)

    covered = [
        solve_mclp(
            tm, demand_weights, pop, vulnerable, n, 15., ["a", "b", "c"],
            f"n={n}", weights,
        ).covered_ratio
        for n in (1, 2, 3)
    ]
    assert covered == sorted(covered)


def test_coverage_pareto_endpoints_optimize_reported_metrics():
    travel = np.array([[1, 99], [99, 1]], dtype=float)
    population = np.array([100, 60], dtype=float)
    vulnerable = np.array([False, True])
    points = coverage_pareto_sweep(
        travel, population, vulnerable, ['efficient', 'equitable'],
        n_facilities=1, threshold_minutes=15, steps=3,
    )

    assert points[0].solution.selected_ids == ['efficient']
    assert points[-1].solution.selected_ids == ['equitable']
    assert points[0].efficiency_metric > points[-1].efficiency_metric
    assert points[-1].equity_metric > points[0].equity_metric
    assert points[0].solution.weights_used is None
    assert len(non_dominated(points)) == 2

def test_hard_tract_coverage_is_a_real_solver_constraint():
    """Increasing the confirmed budget must make a feasible hard target solvable."""
    travel = np.array([[5., 99., 99.], [99., 5., 99.], [99., 99., 5.]])
    population = np.ones(3); demand_weights = np.ones(3)
    vulnerable = np.array([True, False, False])
    infeasible = solve_mclp(travel, demand_weights, population, vulnerable, 2, 15.,
                            ["a", "b", "c"], "hard", None, coverage_target=1.0)
    feasible = solve_mclp(travel, demand_weights, population, vulnerable, 3, 15.,
                          ["a", "b", "c"], "hard", None, coverage_target=1.0)
    assert infeasible.solver_status == "Infeasible" and infeasible.selected_ids == []
    assert feasible.solver_status == "Optimal" and feasible.covered_ratio == 1.0


def test_pareto_sweep_reports_real_solver_progress():
    """The progress callback must receive one event after every completed solve."""
    events = []
    coverage_pareto_sweep(np.array([[1., 99.], [99., 1.]]), np.array([10., 10.]),
                          np.array([True, False]), ["a", "b"], 1, 15., steps=3,
                          progress_callback=lambda label, done, total: events.append((label, done, total)))
    assert events == [("Pareto sweep", 1, 3), ("Pareto sweep", 2, 3), ("Pareto sweep", 3, 3)]

def test_lscp_finds_the_exact_facility_lower_bound():
    """LSCP must return the minimum count instead of incrementally guessing budgets."""
    travel = np.array([[5., 99., 99.], [99., 5., 99.], [99., 99., 5.]])
    solution = solve_lscp(travel, np.ones(3), np.array([True, False, False]), 15.,
                          ["a", "b", "c"], coverage_target=1.0)
    assert solution.solver_status == "Optimal" and len(solution.selected_ids) == 3


def test_lscp_can_guarantee_only_the_vulnerable_demand_group():
    """A vulnerable-only guarantee must retain all demand rows but constrain the selected subset."""
    travel = np.array([[5., 99.], [99., 5.], [99., 5.]])
    vulnerable = np.array([True, False, False])
    solution = solve_lscp(travel, np.array([10., 10., 10.]), vulnerable, 15., ["a", "b"],
                          coverage_target=1.0, coverage_mask=vulnerable)
    assert solution.solver_status == "Optimal" and solution.selected_ids == ["a"]


def test_minimum_threshold_search_uses_observed_matrix_breakpoints():
    """The fixed-budget search must return the exact first feasible travel-time breakpoint."""
    threshold, solution = minimum_coverage_threshold(
        np.array([[5., 20.], [20., 5.]]), np.ones(2), np.array([True, False]),
        ["a", "b"], n_facilities=1, coverage_target=1.0)
    assert threshold == 20.0 and solution is not None and len(solution.selected_ids) == 1
