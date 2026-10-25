"""The solver child must stop even when CBC ignores its own time budget."""
import subprocess
import pulp
import pytest
import numpy as np
from time import monotonic
from oasis.core.cancellation import RunCancelled
from oasis.core.optimization import cbc
from oasis.core.optimization.models import solve_mclp


class HungCBC:
    def __init__(self):
        self.returncode = None
        self.terminated = False
    def __enter__(self):
        return self
    def __exit__(self, *_):
        return False
    def poll(self):
        return self.returncode
    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("cbc", timeout)
        return self.returncode
    def terminate(self):
        self.terminated = True
        self.returncode = -1


def problem():
    model = pulp.LpProblem("test", pulp.LpMinimize)
    x = pulp.LpVariable("x", cat="Binary")
    model += x
    model += x >= 1
    return model


def test_wall_deadline_terminates_unresponsive_solver(monkeypatch):
    child = HungCBC()
    ticks = iter([0, 0, 0, 1, 2, 3])
    heartbeats = []
    monkeypatch.setattr(cbc, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(cbc.subprocess, "Popen", lambda *_args, **_kwargs: child)
    assert cbc.solve_bounded_cbc(problem(), 3, heartbeat=lambda: heartbeats.append(True)) == "Not Solved"
    assert child.terminated
    assert heartbeats == [True, True]


def test_cancellation_terminates_unresponsive_solver(monkeypatch):
    child = HungCBC()
    checks = 0
    def cancel():
        nonlocal checks
        checks += 1
        if checks == 3:
            raise RunCancelled("cancelled")
    monkeypatch.setattr(cbc.subprocess, "Popen", lambda *_args, **_kwargs: child)
    with pytest.raises(RunCancelled):
        cbc.solve_bounded_cbc(problem(), cbc.monotonic() + 30, cancel_check=cancel)
    assert child.terminated


def test_cbc_marks_integer_incumbent_feasible_not_optimal(monkeypatch):
    original = pulp.COIN_CMD.readsol_MPS
    def integer_incumbent(self, *args):
        status, values, reduced, prices, slacks, _ = original(self, *args)
        return status, values, reduced, prices, slacks, pulp.LpSolutionIntegerFeasible
    monkeypatch.setattr(pulp.COIN_CMD, "readsol_MPS", integer_incumbent)
    assert cbc.solve_bounded_cbc(problem(), cbc.monotonic() + 10) == "Feasible"


def test_requested_gradual_solve_terminates_a_hung_cbc_and_reports_activity(monkeypatch):
    child = HungCBC()
    base = monotonic()
    ticks = iter([base, base, base, base + 1, base + 2, base + 4])
    heartbeat = []
    monkeypatch.setattr(cbc, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(cbc.subprocess, "Popen", lambda *_args, **_kwargs: child)
    result = solve_mclp(np.ones((2, 2)), np.ones(2), np.ones(2), np.zeros(2, bool),
                        1, 15, ["a", "b"], "requested", None, time_limit_s=3,
                        heartbeat=lambda: heartbeat.append(True))
    assert child.terminated and heartbeat
    assert result.solver_status == "Not Solved" and result.selected_ids == []


def test_requested_gradual_solve_cancels_its_cbc_child(monkeypatch):
    child = HungCBC()
    started = False
    def start(*_args, **_kwargs):
        nonlocal started
        started = True
        return child
    def cancel():
        if started:
            raise RunCancelled("cancelled")
    monkeypatch.setattr(cbc.subprocess, "Popen", start)
    with pytest.raises(RunCancelled):
        solve_mclp(np.ones((1, 1)), np.ones(1), np.ones(1), np.zeros(1, bool),
                   1, 15, ["a"], "requested", None, cancel_check=cancel)
    assert child.terminated


def test_cbc_disables_parallel_workers(monkeypatch):
    original = cbc.subprocess.Popen
    commands = []
    def start(args, **kwargs):
        commands.append(args)
        return original(args, **kwargs)
    monkeypatch.setattr(cbc.subprocess, "Popen", start)
    assert cbc.solve_bounded_cbc(problem(), monotonic() + 10) == "Optimal"
    assert commands[0][commands[0].index("-threads") + 1] == "0"


def test_system_cbc_is_used_when_available(monkeypatch):
    native_path = pulp.PULP_CBC_CMD().path
    monkeypatch.setattr(cbc, "which", lambda executable: native_path if executable == "cbc" else None)
    def bundled_solver(**kwargs):
        pytest.fail("The system CBC must take precedence over the bundled binary")
    monkeypatch.setattr(pulp, "PULP_CBC_CMD", bundled_solver)
    assert cbc.solve_bounded_cbc(problem(), monotonic() + 10) == "Optimal"
