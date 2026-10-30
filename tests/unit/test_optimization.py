"""Unit tests for the deterministic core. No external API calls.

Purpose: (1) keep the solver verifiable; (2) show reviewers exactly which
behaviors are guaranteed vs model-decided.
"""
import numpy as np
import pytest
from types import SimpleNamespace
import oasis.core.optimization.models as optimization_models
from oasis.contracts.domain import ValueWeights
from oasis.core.optimization.models import (_solve_mclp_selection, build_demand_weights, gradual_coverage_matrix, population_weighted_gini,
                                       minimum_coverage_threshold, solve_lscp, solve_mclp)
from oasis.core.optimization.pareto import coverage_pareto_sweep, non_dominated


def test_value_weights_reject_values_outside_unit_interval():
    with pytest.raises(ValueError, match=r"weights must be in \[0,1\]"):
        ValueWeights(population_density=1.01)


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


def test_mclp_omits_zero_weight_rows_without_changing_the_exact_objective(monkeypatch):
    """Pure-equity Pareto endpoints must not create irrelevant binary demand rows."""
    import pulp

    created = []
    real_variable = pulp.LpVariable

    def tracked_variable(name, *args, **kwargs):
        created.append(name)
        return real_variable(name, *args, **kwargs)

    monkeypatch.setattr(pulp, "LpVariable", tracked_variable)
    chosen, status = _solve_mclp_selection(
        np.array([[True, False], [False, True]]),
        np.array([0.0, 1.0]),
        n_facilities=1,
    )

    assert status == "Optimal" and chosen == [1]
    assert "y_0" not in created and "y_1" in created


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


def test_efficiency_profile_is_pure_population_not_density_squared():
    population = np.array([100.0, 100.0])
    indicators = {"population_density": np.array([1.0, 1000.0])}
    weights = ValueWeights(
        population_density=1., elderly_ratio=0., poverty_rate=0., heat_exposure=0.
    )
    assert np.array_equal(build_demand_weights(indicators, population, weights), population)


def test_equity_coverage_accepts_explicit_group_population():
    solution = solve_mclp(
        np.array([[1.0, 99.0], [99.0, 1.0]]),
        np.array([1.0, 0.0]),
        np.array([100.0, 100.0]),
        np.array([90.0, 10.0]),
        1, 15.0, ["a", "b"], "explicit-equity", None,
    )
    assert solution.selected_ids == ["a"]
    assert np.isclose(solution.vulnerable_covered_ratio, 0.9)


def test_population_weighted_gini_respects_population_weights_and_zero_case():
    assert np.isclose(population_weighted_gini(np.array([0., 1.]), np.array([1., 1.])), .5)
    assert np.isclose(population_weighted_gini(np.array([0., 1.]), np.array([9., 1.])), .9)
    assert population_weighted_gini(np.zeros(2), np.ones(2)) is None


def test_solution_reports_gradual_access_gini():
    solution = solve_mclp(
        np.array([[0.0], [10.0]]), np.ones(2), np.ones(2), np.ones(2),
        1, 20.0, ["a"], "gini", None,
    )
    assert np.isclose(solution.mean_access_score, .75)
    assert np.isclose(solution.population_weighted_access_gini, 1 / 6)
    assert np.isclose(solution.gradual_coverage_ratio, .75)


def test_gradual_coverage_prefers_a_closer_site_within_the_same_threshold():
    """Two binary-covered choices differ when travel-time decay is optimized."""
    solution = solve_mclp(
        np.array([[14.0, 2.0]]), np.ones(1), np.array([100.0]), np.array([100.0]),
        1, 15.0, ["far", "near"], "gradual", None,
    )
    assert solution.selected_ids == ["near"]
    assert np.isclose(gradual_coverage_matrix(np.array([[2.0]]), 15.0)[0, 0], 13 / 15)


def test_capacity_assigns_demand_and_selects_enough_supply():
    solution = solve_mclp(
        np.array([[1.0, 1.0], [1.0, 1.0]]), np.ones(2),
        np.array([80.0, 80.0]), np.array([40.0, 40.0]),
        2, 15.0, ["a", "b"], "capacity", None,
        facility_capacities=np.array([100.0, 100.0]),
    )
    assert len(solution.selected_ids) == 2
    assert np.isclose(solution.assigned_population, 160.0)
    assert all(load <= 100.0 + 1e-6 for load in solution.facility_loads.values())
    assert np.isclose(solution.maximum_capacity_utilization, .8)


def test_capacity_can_leave_population_partially_unassigned():
    solution = solve_mclp(
        np.array([[1.0]]), np.ones(1), np.array([100.0]), np.array([50.0]),
        1, 15.0, ["a"], "capacity", None,
        facility_capacities=np.array([40.0]),
    )
    assert np.isclose(solution.covered_ratio, .4)
    assert np.isclose(solution.assigned_population, 40.0)


def test_assignment_weighted_travel_time_respects_capacity_rerouting():
    solution = solve_mclp(
        np.array([[1.0, 9.0]]), np.ones(1), np.array([100.0]), np.array([50.0]),
        2, 15.0, ["near", "far"], "capacity", None,
        facility_capacities=np.array([40.0, 100.0]),
    )
    assert np.isclose(solution.mean_travel_minutes, 5.8)


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


def test_efficiency_gini_pareto_uses_gradual_access_and_actual_gini():
    travel = np.array([[1, 99], [14, 1], [14, 1]], dtype=float)
    population = np.array([100, 60, 60], dtype=float)
    vulnerable = np.array([False, True, True])
    points = coverage_pareto_sweep(
        travel, population, vulnerable, ['efficient', 'equitable'],
        n_facilities=1, threshold_minutes=15, steps=3,
    )

    assert points[0].efficiency_metric == points[0].solution.gradual_coverage_ratio
    assert all(
        np.isclose(point.equity_metric, 1 - point.population_weighted_gini)
        for point in points if point.population_weighted_gini is not None
    )
    assert points[0].solution.weights_used is None
    assert non_dominated(points)


def test_task_rejects_simultaneous_alpha_and_hard_coverage_target():
    import pytest
    from oasis.contracts.domain import TaskSpec

    with pytest.raises(ValueError, match="equity_alpha"):
        TaskSpec(place_name="Phoenix", equity_alpha=.5, coverage_target=.8)

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
    assert events == [("Efficiency--Gini sweep", 1, 3), ("Efficiency--Gini sweep", 2, 3), ("Efficiency--Gini sweep", 3, 3)]

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


def test_minimum_threshold_accepts_a_budget_larger_than_the_candidate_set():
    matrix = np.array([[3., 8.], [8., 4.]])
    threshold, solution = minimum_coverage_threshold(
        matrix, np.ones(2), np.zeros(2, bool), ["a", "b"], n_facilities=10, time_limit_s=5)
    assert threshold == 4 and len(solution.selected_ids) == 2


def test_minimum_threshold_search_uses_observed_matrix_breakpoints():
    """The fixed-budget search must return the exact first feasible travel-time breakpoint."""
    threshold, solution = minimum_coverage_threshold(
        np.array([[5., 20.], [20., 5.]]), np.ones(2), np.array([True, False]),
        ["a", "b"], n_facilities=1, coverage_target=1.0)
    assert threshold == 20.0 and solution is not None and len(solution.selected_ids) == 1
def test_minimum_threshold_search_has_one_total_budget_and_pre_solve_progress(monkeypatch):
    """Repeated LSCP probes must share one deadline and report before blocking."""
    ticks = iter([0.0, 0.0, 100.0, 100.0])
    probe_limits = []
    events = []

    monkeypatch.setattr(optimization_models, "monotonic", lambda: next(ticks))

    def fake_lscp(*args, **kwargs):
        probe_limits.append(args[-1])
        return SimpleNamespace(solver_status="Infeasible", selected_ids=[])

    monkeypatch.setattr(optimization_models, "solve_lscp", fake_lscp)
    diagnostics = {}
    threshold, solution = minimum_coverage_threshold(
        np.array([[5.0, 20.0]]), np.ones(1), np.array([True]), ["a", "b"],
        n_facilities=1, time_limit_s=120, total_time_limit_s=10,
        progress_callback=lambda label, done, total: events.append((label, done, total)),
        search_diagnostics=diagnostics,
    )

    assert threshold is None and solution is None
    assert probe_limits == [5]
    assert events[0] == ("Minimum-threshold search", 0, 2)
    assert events[-1] == ("Minimum-threshold search", 1, 2)
    assert diagnostics["status"] == "time_limit"
    assert diagnostics["probes"] == 1


def test_threshold_search_does_not_treat_large_feasible_incumbent_as_infeasible(monkeypatch):
    calls = []
    def timed_lscp(*args, **kwargs):
        calls.append(args[3])
        return SimpleNamespace(solver_status="Feasible", selected_ids=["a", "b"])
    monkeypatch.setattr(optimization_models, "solve_lscp", timed_lscp)
    diagnostics = {}
    threshold, solution = minimum_coverage_threshold(
        np.array([[5., 20.], [20., 5.]]), np.ones(2), np.zeros(2, bool),
        ["a", "b"], 1, search_diagnostics=diagnostics,
    )
    assert threshold is None and solution is None
    assert diagnostics["status"] == "solver_time_limit"
    assert len(calls) == 1


def test_lscp_checks_cancellation_during_model_construction():
    from oasis.core.cancellation import RunCancelled
    calls = 0
    def cancel():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RunCancelled("cancelled")
    with pytest.raises(RunCancelled):
        solve_lscp(np.ones((5, 2)), np.ones(5), np.zeros(5, bool), 10, ["a", "b"],
                   cancel_check=cancel)
    assert calls == 3
