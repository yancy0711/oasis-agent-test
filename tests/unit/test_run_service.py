"""Durable mode, cancellation, and revision tests for the run service."""
from __future__ import annotations

import multiprocessing
import threading
import time
from pathlib import Path

from oasis.agent.context import Context
from oasis.contracts.domain import TaskSpec
from oasis.services.application import OasisService
from oasis.services.run_store import RunStore


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


def test_precise_mode_rejects_silent_router_fallback(tmp_path, monkeypatch):
    """A precise result cannot be labelled successful after Haversine fallback."""
    service = _service(tmp_path, monkeypatch, _FallbackOrchestrator)
    try:
        run_id = service.start_run(TaskSpec(place_name="Phoenix"), "precise")["run_id"]
        snapshot = _terminal(service, run_id)
        assert snapshot["status"] == "failed"
        assert "requires ORS network routing" in snapshot["error"]
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
    first.append_event("run-1", {"stage": "plan", "message": "queued"})

    second = RunStore(path)
    assert second.interrupt_active() == 1
    snapshot = second.snapshot("run-1")
    assert snapshot["status"] == "interrupted"
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
