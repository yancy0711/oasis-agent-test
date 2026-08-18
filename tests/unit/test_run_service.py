"""Durable mode, cancellation, and revision tests for the run service."""
from __future__ import annotations

import json
import multiprocessing
import threading
import time
from pathlib import Path

import pytest

from oasis.agent.context import Context
from oasis.contracts.domain import ParetoPoint, SiteSolution, TaskSpec
from oasis.services.application import OasisService
from oasis.services.run_store import RunStore


def test_public_llm_endpoints_are_fixed_or_explicitly_approved(monkeypatch):
    service = object.__new__(OasisService)
    service.cfg = {"llm": {"allowed_custom_hosts": []}}
    assert service._validated_llm_url("openai", "https://attacker.example/v1", False) == "https://api.openai.com/v1"
    monkeypatch.setattr("oasis.services.application.socket.getaddrinfo", lambda *_: [(None, None, None, None, ("8.8.8.8", 443))])
    with pytest.raises(ValueError, match="not approved"):
        service._validated_llm_url("custom", "https://models.example/v1", False)
    service.cfg["llm"]["allowed_custom_hosts"] = ["models.example"]
    assert service._validated_llm_url("custom", "https://models.example/v1", False) == "https://models.example/v1"


def test_remote_visitors_cannot_reach_private_model_endpoints(monkeypatch):
    service = object.__new__(OasisService)
    service.cfg = {"llm": {"allowed_custom_hosts": ["internal.example"]}}
    monkeypatch.setattr("oasis.services.application.socket.getaddrinfo", lambda *_: [(None, None, None, None, ("127.0.0.1", 443))])
    with pytest.raises(ValueError, match="Private or loopback"):
        service._validated_llm_url("custom", "https://internal.example/v1", False)


def test_connection_success_requires_a_completion_from_the_selected_model(monkeypatch):
    seen = {}
    class ProbeClient:
        def __init__(self, config):
            seen.update(config)
        def probe(self, _system, _user):
            return {
                "place_name": "Phoenix, Arizona, USA",
                "analysis_goal": "facility_siting",
                "n_facilities": 2,
                "threshold_minutes": 15,
            }, "deepseek/deepseek-v4-flash-202608"

    service = object.__new__(OasisService)
    service.cfg = {"llm": {"max_tokens": 1024}}
    service.lock = threading.Lock()
    service.llm_sessions = {}
    service._validated_llm_url = lambda *_: "https://openrouter.ai/api/v1"
    monkeypatch.setattr("oasis.services.application.LLMClient", ProbeClient)

    result = service.test_llm_connection(
        "openrouter", "https://openrouter.ai/api/v1",
        "deepseek/deepseek-v4-flash-latest", "test-key",
    )

    assert seen["model"] == "deepseek/deepseek-v4-flash-latest"
    assert result["resolved_model"] == "deepseek/deepseek-v4-flash-202608"
    assert result["structured_output"] is True
    stored = service.llm_sessions[result["session_id"]]["config"]
    assert stored["max_tokens"] == 1024


class _RoutingOrchestrator:
    """Return the router declared by the phase configuration."""

    def __init__(self, llm, cfg):
        self.cfg = cfg
        self.ctx = Context()

    def run(self, task):
        router = self.cfg["accessibility"]["router"]
        used = "ors_sparse" if router == "ors" else "haversine"
        report = Path(self.cfg["output"]["dir"]) / "report.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("Test report.", encoding="utf-8")
        return {
            "ok": True,
            "paused": False,
            "message": "done",
            "failed_tool": None,
            "results": [{
                "tool": "build_travel_matrix",
                "ok": True,
                "summary": {"router": used, "tier": "live"},
                "provenance": {"tier": "live"},
            }],
            "lineage": [],
            "outputs": {"report": str(report)},
            "heat_summary": None,
            "diagnostics": {},
            "narrative": None,
            "recovery": None,
            "history": [],
        }


class _FallbackOrchestrator(_RoutingOrchestrator):
    """Simulate an invalid Haversine fallback during a precise phase."""

    def run(self, task):
        payload = super().run(task)
        payload["results"][0]["summary"]["router"] = "haversine"
        return payload


class _LargePreviewOrchestrator(_RoutingOrchestrator):
    """Publish a quick matrix that is too large for automatic ORS refinement."""

    def run(self, task):
        payload = super().run(task)
        payload["results"][0]["summary"]["mode_reports"] = {
            "walking": {"total_pairs": 500000, "eligible_pairs": 250001}
        }
        return payload


class _PrunedLargePreviewOrchestrator(_RoutingOrchestrator):
    """A large full matrix may proceed when distance pruning keeps ORS work bounded."""

    def run(self, task):
        payload = super().run(task)
        payload["results"][0]["summary"]["mode_reports"] = {
            "walking": {"total_pairs": 471912, "eligible_pairs": 12000}
        }
        return payload


class _ParetoOrchestrator(_RoutingOrchestrator):
    """Publish a compact frontier through the public run result."""

    def run(self, task):
        payload = super().run(task)
        solution = SiteSolution(profile_name="alpha", selected_ids=["site-a"],
                                covered_population=80, covered_ratio=.8,
                                vulnerable_covered_ratio=.7, population_weighted_access_gini=.2,
                                mean_access_score=.6, gradual_coverage_ratio=.6,
                                mean_travel_minutes=8, weights_used=None, solver_status="Optimal")
        point = ParetoPoint(alpha=.5, efficiency_metric=.6, equity_metric=.8,
                            population_weighted_gini=.2, solution=solution)
        self.ctx.put("analysis", {"solution": solution, "points": [point], "frontier": [point]})
        return payload


def _service(tmp_path, monkeypatch, orchestrator=_RoutingOrchestrator) -> OasisService:
    """Create an isolated service with no provider calls."""
    monkeypatch.setattr("oasis.services.application.Orchestrator", orchestrator)
    cfg = {
        "llm": {},
        "accessibility": {},
        "optimization": {},
        "runtime": {"run_db": str(tmp_path / "runs.sqlite")},
        "output": {"dir": str(tmp_path / "outputs")},
    }
    return OasisService(cfg=cfg, llm_factory=lambda: object())


def _terminal(service: OasisService, run_id: str) -> dict:
    """Wait briefly for one deterministic fake run."""
    deadline = time.monotonic() + 3
    while True:
        snapshot = service.run_snapshot(run_id)
        if snapshot["status"] in {
            "cancelled", "superseded", "interrupted", "succeeded", "awaiting_hitl", "failed"
        }:
            return snapshot
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_auto_keeps_quick_preview_and_finishes_with_precise_provenance(tmp_path, monkeypatch):
    """Auto mode must preserve both accuracy phases and a stable spec hash."""
    service = _service(tmp_path, monkeypatch)
    try:
        run_id = service.start_run(TaskSpec(place_name="Phoenix"), "auto")["run_id"]
        snapshot = _terminal(service, run_id)
        assert snapshot["status"] == "succeeded"
        assert snapshot["preview_result"]["execution"]["phase"] == "quick"
        assert snapshot["preview_result"]["execution"]["accuracy"] == "approximate"
        assert snapshot["result"]["execution"]["phase"] == "precise"
        assert snapshot["result"]["execution"]["router_used"] == "ors_sparse"
        quick_report = service.artifact(run_id, "preview_report").read_text(encoding="utf-8")
        precise_report = service.artifact(run_id, "report").read_text(encoding="utf-8")
        assert "Approximate preview (Haversine)" in quick_report
        assert "Precise network result (ORS)" in precise_report
        assert len(snapshot["spec"]["spec_hash"]) == 64
    finally:
        service.executor.shutdown(wait=True)


def test_auto_retains_approximate_result_when_precise_routing_fails(tmp_path, monkeypatch):
    """An ORS failure must not discard a completed approximate map and report."""
    service = _service(tmp_path, monkeypatch, _FallbackOrchestrator)
    try:
        run_id = service.start_run(TaskSpec(place_name="New York"), "auto")["run_id"]
        snapshot = _terminal(service, run_id)
        assert snapshot["status"] == "succeeded"
        assert snapshot["result"] is None
        assert snapshot["preview_result"]["ok"] is True
        assert snapshot["preview_result"]["execution"]["accuracy"] == "approximate"
        assert "approximate selection result has been retained" in snapshot["preview_result"]["message"]
        assert "Point route planning remains available" in snapshot["preview_result"]["message"]
        assert "Precise mode requires ORS" in snapshot["error"]
    finally:
        service.executor.shutdown(wait=True)


def test_auto_skips_precise_phase_when_quick_pair_count_exceeds_limit(tmp_path, monkeypatch):
    """Large metros must return their preview without repeating an impossible phase."""
    service = _service(tmp_path, monkeypatch, _LargePreviewOrchestrator)
    service.cfg["accessibility"]["ors_max_eligible_pairs"] = 250000
    try:
        run_id = service.start_run(TaskSpec(place_name="New York"), "auto")["run_id"]
        snapshot = _terminal(service, run_id)
        assert snapshot["status"] == "succeeded"
        assert snapshot["preview_result"]["execution"]["accuracy"] == "approximate"
        assert "automatic ORS preflight limit" in snapshot["error"]
    finally:
        service.executor.shutdown(wait=True)


def test_auto_uses_eligible_pairs_not_full_matrix_size_for_ors_preflight(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch, _PrunedLargePreviewOrchestrator)
    service.cfg["accessibility"]["ors_max_eligible_pairs"] = 250000
    try:
        run_id = service.start_run(TaskSpec(place_name="Phoenix"), "auto")["run_id"]
        snapshot = _terminal(service, run_id)
        assert snapshot["status"] == "succeeded"
        assert snapshot["result"]["execution"]["phase"] == "precise"
        assert snapshot["result"]["execution"]["router_used"] == "ors_sparse"
    finally:
        service.executor.shutdown(wait=True)


def test_run_result_exposes_compact_interactive_pareto_points(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch, _ParetoOrchestrator)
    try:
        run_id = service.start_run(TaskSpec(place_name="Phoenix"), "quick")["run_id"]
        point = _terminal(service, run_id)["result"]["pareto_points"][0]
        assert point["alpha"] == .5 and point["gini"] == .2
        assert point["selected_ids"] == ["site-a"] and point["is_frontier"] is True
    finally:
        service.executor.shutdown(wait=True)


@pytest.mark.parametrize("status", ["running", "succeeded"])
def test_point_route_uses_visible_quick_preview(tmp_path, monkeypatch, status):
    """The planner uses the visible preview during refinement and after fallback."""
    service = _service(tmp_path, monkeypatch)
    run_id = "preview-route"
    task = TaskSpec(place_name="Phoenix")
    service.store.create(run_id, {"task": task.model_dump(mode="json"), "mode": "auto"})
    service.store.update(
        run_id,
        status=status,
        preview_result={"artifacts": {"map": f"/v1/runs/{run_id}/artifacts/preview_map"}},
    )
    preview_map = tmp_path / "preview_map.geojson"
    preview_map.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [[
                    [-112.1, 33.4], [-112.0, 33.4], [-112.0, 33.5],
                    [-112.1, 33.5], [-112.1, 33.4],
                ]]},
                "properties": {"layer_kind": "tract", "GEOID": "1", "tract_mean_heat_c": 35},
            },
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-112.05, 33.45]},
                "properties": {"layer_kind": "selected_site", "candidate_id": "site-1"},
            },
        ],
    }), encoding="utf-8")
    service.store.put_artifacts(run_id, "quick", {"preview_map": preview_map})
    captured = {}

    def fake_route(longitude, latitude, areas, candidates, selected_ids, **kwargs):
        captured.update(longitude=longitude, latitude=latitude, selected_ids=selected_ids)
        return {"type": "FeatureCollection", "features": [], "summary": {"destination_id": "site-1"}}

    monkeypatch.setattr("oasis.services.application.plan_point_to_nearest_site", fake_route)
    try:
        result = service.plan_point_route(run_id, -112.05, 33.45, "walking")
        assert result["summary"]["destination_id"] == "site-1"
        assert captured["selected_ids"] == ["site-1"]
    finally:
        service.executor.shutdown(wait=True)


def test_place_route_rejects_geocoded_origin_outside_study_area(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    run_id = "place-route"
    task = TaskSpec(place_name="Phoenix")
    service.store.create(run_id, {"task": task.model_dump(mode="json"), "mode": "quick"})
    service.store.update(run_id, status="succeeded", result={"artifacts": {}})
    map_path = tmp_path / "map.geojson"
    map_path.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [[
                [-112.1, 33.4], [-112.0, 33.4], [-112.0, 33.5],
                [-112.1, 33.5], [-112.1, 33.4],
            ]]},
            "properties": {"layer_kind": "tract", "GEOID": "1"},
        }],
    }), encoding="utf-8")
    service.store.put_artifacts(run_id, "quick", {"map": map_path})
    monkeypatch.setattr(
        "oasis.services.application.search_origin_candidates",
        lambda query: [{"display_name": "Hong Kong", "lon": "114.17", "lat": "22.32"}],
    )
    try:
        with pytest.raises(ValueError, match="outside the current study area"):
            service.plan_place_route(run_id, "Hong Kong", "walking")
    finally:
        service.executor.shutdown(wait=True)


def test_precise_mode_returns_an_explicit_approximate_fallback(tmp_path, monkeypatch):
    """A failed precise refinement retains a clearly labelled approximate result."""
    service = _service(tmp_path, monkeypatch, _FallbackOrchestrator)
    try:
        run_id = service.start_run(TaskSpec(place_name="Phoenix"), "precise")["run_id"]
        snapshot = _terminal(service, run_id)
        assert snapshot["status"] == "succeeded"
        assert "requires ORS network routing" in snapshot["error"]
        assert snapshot["result"]["execution"]["accuracy"] == "approximate"
        assert "approximate selection result has been retained" in snapshot["result"]["message"]
        assert "Point route planning remains available" in snapshot["result"]["message"]
    finally:
        service.executor.shutdown(wait=True)


def test_queued_cancel_is_idempotent_and_revision_is_immutable(tmp_path, monkeypatch):
    """Queued work can be cancelled twice and revised into a hashed child spec."""
    service = _service(tmp_path, monkeypatch)
    release = threading.Event()
    blocker = service.executor.submit(release.wait)
    try:
        parent = service.start_run(TaskSpec(place_name="Phoenix"), "quick")["run_id"]
        first = service.cancel_run(parent)
        second = service.cancel_run(parent)
        assert first["status"] in {"cancelling", "cancelled"}
        assert second["status"] in {"cancelling", "cancelled"}
        assert _terminal(service, parent)["status"] == "cancelled"

        old = service.start_run(TaskSpec(place_name="Phoenix", n_facilities=2), "quick")["run_id"]
        child = service.revise_run(
            old, TaskSpec(place_name="Phoenix", n_facilities=3), "quick"
        )
        assert _terminal(service, old)["status"] == "superseded"
        child_snapshot = service.run_snapshot(child["run_id"])
        assert child_snapshot["parent_run_id"] == old
        assert child_snapshot["revision"] == 1
        assert child_snapshot["task"]["n_facilities"] == 3
        assert len(child_snapshot["spec"]["spec_hash"]) == 64
    finally:
        release.set()
        blocker.result(timeout=2)
        service.executor.shutdown(wait=True)


def test_run_store_survives_reopen_and_marks_lost_work_interrupted(tmp_path):
    """Persistent state is readable by a new process and active work is not replayed."""
    path = tmp_path / "runs.sqlite"
    first = RunStore(path)
    first.create("run-1", {"mode": "quick", "task": {"place_name": "Phoenix"}})
    first.transition(
        "run-1", ("queued",), "running",
        progress={"label": "Old stage", "done": 1, "total": 1},
    )
    first.append_event("run-1", {"stage": "plan", "message": "queued"})

    second = RunStore(path)
    assert second.interrupt_active() == 1
    snapshot = second.snapshot("run-1")
    assert snapshot["status"] == "interrupted"
    assert snapshot["progress"] is None
    assert snapshot["events"][0]["message"] == "queued"

def test_waiting_revision_is_visible_and_cancellable(tmp_path, monkeypatch):
    """A serialized run reports why it is waiting and can exit before acquiring the lock."""
    service = _service(tmp_path, monkeypatch)
    service.execution_lock.acquire()
    try:
        run_id = service.start_run(
            TaskSpec(place_name="Phoenix", mobility_scenario="driving"), "auto"
        )["run_id"]
        deadline = time.monotonic() + 2
        while True:
            snapshot = service.run_snapshot(run_id)
            if snapshot["status"] == "running" and snapshot.get("progress"):
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert snapshot["progress"]["label"] == "Waiting for the previous run to stop"
        assert snapshot["task"]["mobility_scenario"] == "driving"

        service.cancel_run(run_id)
        assert _terminal(service, run_id)["status"] == "cancelled"
    finally:
        if service.execution_lock.locked():
            service.execution_lock.release()
        service.executor.shutdown(wait=True)

def test_hard_cancel_terminates_an_isolated_run_process():
    """Whole-run cancellation must not wait for a cooperative stage checkpoint."""
    process = multiprocessing.get_context("spawn").Process(target=time.sleep, args=(30,))
    process.start()
    started = time.monotonic()

    OasisService._terminate_process(process)

    assert not process.is_alive()
    assert time.monotonic() - started < 6
