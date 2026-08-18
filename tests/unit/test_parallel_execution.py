"""Offline tests for bounded parallel execution and cooperative checkpoints."""
from __future__ import annotations

from threading import Event, Lock
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Point, box

from oasis.agent.context import Context
from oasis.contracts.domain import Provenance
from oasis.core.cache import current_cache_scope
from oasis.core.cancellation import RunCancelled
from oasis.core.data import heat
from oasis.core.optimization import models, pareto
from oasis.services import workflow


def _pareto_inputs():
    travel = np.array([[1.0, 99.0], [99.0, 1.0]])
    population = np.array([100.0, 60.0])
    vulnerable = np.array([False, True])
    return travel, population, vulnerable, ["efficient", "equitable"]


def test_parallel_pareto_matches_serial_and_checkpoints_in_parent():
    """Spawned CBC solves must preserve deterministic, alpha-sorted results."""
    args = _pareto_inputs()
    serial = pareto.coverage_pareto_sweep(
        *args,
        n_facilities=1,
        threshold_minutes=15,
        steps=3,
    )
    checkpoints = []
    progress = []
    parallel = pareto.coverage_pareto_sweep(
        *args,
        n_facilities=1,
        threshold_minutes=15,
        steps=3,
        max_workers=2,
        checkpoint_callback=lambda index, point: checkpoints.append(
            (index, point.alpha)
        ),
        progress_callback=lambda label, done, total: progress.append(
            (label, done, total)
        ),
    )

    assert [point.model_dump() for point in parallel] == [
        point.model_dump() for point in serial
    ]
    assert [point.alpha for point in parallel] == [0.0, 0.5, 1.0]
    assert sorted(checkpoints) == [(0, 0.0), (1, 0.5), (2, 1.0)]
    assert [done for _, done, _ in progress] == [1, 2, 3]


def test_pareto_resume_skips_completed_points(monkeypatch):
    """A complete checkpoint must return without launching another solver."""
    args = _pareto_inputs()
    completed = pareto.coverage_pareto_sweep(
        *args,
        n_facilities=1,
        threshold_minutes=15,
        steps=3,
    )
    monkeypatch.setattr(
        pareto,
        "_gini_aware_solve",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("checkpointed solve reran")
        ),
    )
    progress = []
    resumed = pareto.coverage_pareto_sweep(
        *args,
        n_facilities=1,
        threshold_minutes=15,
        steps=3,
        completed_points=dict(enumerate(completed)),
        progress_callback=lambda label, done, total: progress.append(
            (label, done, total)
        ),
    )
    assert resumed == completed
    assert progress == [("Efficiency--Gini sweep", 3, 3)]


def test_pareto_cancel_keeps_the_last_completed_checkpoint():
    """Cancellation is observed between solves after the finished point is saved."""
    args = _pareto_inputs()
    cancelled = False
    checkpoints = []

    def check_cancelled():
        if cancelled:
            raise RunCancelled("test cancellation")

    def checkpoint(index, point):
        nonlocal cancelled
        checkpoints.append((index, point))
        cancelled = True

    with pytest.raises(RunCancelled):
        pareto.coverage_pareto_sweep(
            *args,
            n_facilities=1,
            threshold_minutes=15,
            steps=3,
            cancel_check=check_cancelled,
            checkpoint_callback=checkpoint,
        )
    assert [index for index, _ in checkpoints] == [0]


def test_mclp_explicitly_runs_cbc_with_one_thread(monkeypatch):
    """Process-level Pareto parallelism must not nest CBC thread pools."""
    seen = []
    original = models.pulp.PULP_CBC_CMD

    def command(*args, **kwargs):
        seen.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(models.pulp, "PULP_CBC_CMD", command)
    models.solve_mclp(
        np.array([[1.0, 99.0], [99.0, 1.0]]),
        np.array([1.0, 2.0]),
        np.array([10.0, 10.0]),
        np.array([False, True]),
        1,
        15,
        ["a", "b"],
        "test",
        None,
    )
    assert seen and seen[-1]["threads"] == 1


def test_landsat_header_probes_are_bounded_and_preserve_order(monkeypatch):
    """Independent scene probes may overlap but never exceed eight workers."""
    items = [SimpleNamespace(id=str(index)) for index in range(10)]
    lock = Lock()
    release = Event()
    active = 0
    peak = 0

    def probe(item):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active >= 2:
                release.set()
        try:
            assert release.wait(2)
            return item.id != "3"
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(heat, "_item_is_readable", probe)
    result = heat._readable_items(items, max_workers=99)
    assert [item.id for item in result] == [
        str(index) for index in range(10) if index != 3
    ]
    assert 2 <= peak <= 8


def test_heat_cancellation_stops_between_cached_stages(monkeypatch, tmp_path):
    """A cancellation after STAC search must prevent later raster work."""
    tracts = gpd.GeoDataFrame(
        {"GEOID": ["06037101100"]},
        geometry=[Point(-118, 34)],
        crs="EPSG:4326",
    )
    checks = 0
    monkeypatch.setattr(heat, "cache_dir", lambda namespace: tmp_path)
    monkeypatch.setattr(heat, "_search_items", lambda *args: ([object()], "live"))
    monkeypatch.setattr(
        heat,
        "_readable_items",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("raster validation ran after cancellation")
        ),
    )

    def cancel_check():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RunCancelled("test cancellation")

    with pytest.raises(RunCancelled):
        heat.fetch_heat_exposure(
            tracts,
            "2022-06-01",
            "2022-08-31",
            cancel_check=cancel_check,
        )


def test_county_sources_run_in_scoped_threads_and_join_deterministically(
    monkeypatch,
):
    """Two sources per county use explicit cache scope and ordered fan-in."""
    ctx = Context()
    ctx.put(
        "config",
        {
            "data": {
                "io_workers": 4,
                "landsat_probe_workers": 7,
                "dask_workers": 3,
            }
        },
    )
    ctx.put(
        "study",
        {
            "state_fips": "36",
            "county_fips": "001",
            "county_fips_list": ["001", "003"],
            "year": 2022,
            "cache_scope": "test_city",
            "place_type": "city",
            "boundary": gpd.GeoDataFrame(
                geometry=[box(-1, -1, 3, 2)],
                crs="EPSG:4326",
            ),
        },
    )
    lock = Lock()
    release = Event()
    active = 0
    peak = 0
    scopes = []

    def enter_source():
        nonlocal active, peak
        scope = current_cache_scope()
        with lock:
            scopes.append(scope)
            active += 1
            peak = max(peak, active)
            if active >= 2:
                release.set()
        assert release.wait(2)

    def leave_source():
        nonlocal active
        with lock:
            active -= 1

    def fake_acs(state, county, groups, year):
        enter_source()
        try:
            return pd.DataFrame(
                {
                    "GEOID": [state + county + "000100"],
                    "total_population": [100],
                    "m65_66": [10],
                    "below_poverty": [5 if county == "001" else 10],
                    "poverty_universe": [100],
                }
            )
        finally:
            leave_source()

    def fake_tracts(state, county, year):
        enter_source()
        try:
            x = 0 if county == "001" else 1
            return gpd.GeoDataFrame(
                {
                    "GEOID": [state + county + "000100"],
                    "ALAND": [100_000],
                },
                geometry=[box(x, 0, x + 0.5, 0.5)],
                crs="EPSG:4326",
            )
        finally:
            leave_source()

    heat_options = {}

    def fake_heat(areas, *args, **kwargs):
        heat_options.update(kwargs)
        return (
            pd.DataFrame(
                {
                    "GEOID": areas["GEOID"].astype(str).tolist(),
                    "mean_lst": [30.0, 31.0],
                }
            ),
            Provenance(source="fake heat", record_count=2),
        )

    monkeypatch.setattr(workflow, "fetch_acs", fake_acs)
    monkeypatch.setattr(workflow, "fetch_tract_geometry", fake_tracts)
    monkeypatch.setattr(workflow, "fetch_heat_exposure", fake_heat)
    result = workflow.prepare_demand(ctx)

    assert result.ok, result.error
    assert scopes == ["test_city"] * 4
    assert 2 <= peak <= 4
    assert ctx.get("demand")["areas"]["GEOID"].tolist() == [
        "36001000100",
        "36003000100",
    ]
    assert heat_options["readable_workers"] == 7
    assert heat_options["dask_workers"] == 3
    assert heat_options["cancel_check"] == ctx.check_cancelled

def test_pareto_cancellation_terminates_isolated_solver_tree(monkeypatch):
    """Cancellation kills only the Pareto worker tree and never waits for CBC."""
    calls = []

    class FakeProcess:
        pid = 12345
        terminated = False
        joined = False

        def is_alive(self):
            return True

        def terminate(self):
            self.terminated = True

        def join(self, timeout=None):
            self.joined = timeout == 1

    class FakeExecutor:
        def __init__(self, process):
            self._processes = {process.pid: process}
            self.shutdown_args = None

        def shutdown(self, **kwargs):
            self.shutdown_args = kwargs

    process = FakeProcess()
    executor = FakeExecutor(process)
    monkeypatch.setattr(
        pareto.subprocess,
        "run",
        lambda args, **kwargs: calls.append((args, kwargs)),
    )

    pareto._terminate_executor(executor)

    if pareto.os.name == "nt":
        assert calls and calls[0][0] == [
            "taskkill", "/PID", "12345", "/T", "/F"
        ]
    else:
        assert process.terminated
    assert process.joined
    assert executor.shutdown_args == {"wait": False, "cancel_futures": True}
