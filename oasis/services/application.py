"""Application services and a bounded in-memory run queue for the HTTP API."""
from __future__ import annotations
import hashlib
import json
import multiprocessing
import os
import signal
import subprocess
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any
from uuid import uuid4

from fastapi.encoders import jsonable_encoder

from oasis.agent.orchestrator import LLMClient, MissingPlaceError, Orchestrator
from oasis.contracts.domain import (AgentEvent, ExecutionProvenance, RoutingMode,
                                    RunSpec, TaskSpec)
from oasis.core.cancellation import CancellationToken, RunCancelled
from oasis.settings import PROJECT_ROOT, load_cfg
from oasis.settings import load_profiles
from oasis.core.outputs.report import render_diagnostic_report
from oasis.core.data.geocoding import (AmbiguousPlaceError, cache_place_candidates,
                                               canonical_place_name, select_place_candidate)
from oasis.services.run_store import RunStore


@dataclass
class _RunControl:
    """Keep non-persistable cancellation state for one live run."""
    token: CancellationToken
    future: Future | None = None
    terminal_status: str = "cancelled"
    process: multiprocessing.Process | None = None
    termination_lock: Lock = field(default_factory=Lock)


def _run_process(
    cfg: dict, run_id: str, task_data: dict, spec_data: dict
) -> None:
    """Execute one run in a killable child process that writes durable state directly."""
    if os.name != "nt":
        os.setsid()
    service = OasisService(cfg=cfg, process_isolation=False, worker_mode=True)
    service.controls[run_id] = _RunControl(CancellationToken())
    service._run_serial(run_id, TaskSpec(**task_data), RunSpec(**spec_data))


class OasisService:
    """Own parsing, place resolution, jobs, backend files, and agent credentials."""

    def __init__(
        self,
        cfg: dict | None = None,
        llm_factory=None,
        max_workers: int = 2,
        *,
        process_isolation: bool | None = None,
        worker_mode: bool = False,
    ):
        """Create a service whose production runs execute in killable child processes."""
        self.cfg = cfg or load_cfg(); self.profiles = load_profiles()
        self.llm_factory = llm_factory or (lambda: LLMClient(self.cfg["llm"]))
        self.process_isolation = (
            llm_factory is None if process_isolation is None else process_isolation
        )
        self.executor = (
            None if worker_mode
            else ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="oasis")
        )
        self.lock = Lock()
        self.execution_lock = Lock()
        self.resolutions: dict[str, dict] = {}; self.uploads: dict[str, Path] = {}
        self.upload_root = PROJECT_ROOT / "tmp" / "uploads"; self.upload_root.mkdir(parents=True, exist_ok=True)
        for path in self.upload_root.glob("*.csv"):
            upload_id = path.name.split("_", 1)[0]
            if len(upload_id) == 32 and all(char in "0123456789abcdef" for char in upload_id.lower()):
                self.uploads[upload_id] = path
        output_root = Path(self.cfg["output"]["dir"])
        state_path = Path(self.cfg.get("runtime", {}).get("run_db", output_root / "runs.sqlite"))
        self.store = RunStore(state_path)
        if not worker_mode:
            self.store.interrupt_active()
        self.controls: dict[str, _RunControl] = {}
        self.progress_clocks: dict[str, tuple[str, float, int]] = {}

    def parse(self, prompt: str) -> dict:
        """Parse a task draft while preserving missing-place HITL metadata."""
        orch = Orchestrator(self.llm_factory(), {**self.cfg, "value_profiles": self.profiles})
        message = ""; missing_place = False
        try: task = orch.parse_task(prompt).model_dump(mode="json")
        except MissingPlaceError as exc:
            task, message, missing_place = exc.task_data, str(exc), True
        return {"task": task, "missing_place": missing_place, "message": message,
                "events": [event.model_dump(mode="json") for event in orch.ctx.history]}

    @staticmethod
    def _candidate(row: dict, index: int) -> dict:
        """Return compact public metadata for one retained geocoder candidate."""
        identity = f"{index}:{row.get('osm_type', 'x')}:{row.get('osm_id', 'x')}"
        canonical = canonical_place_name(row); kind = row.get("addresstype", "unknown")
        return {"candidate_id": identity, "canonical_name": canonical,
                "display_name": row.get("display_name", canonical), "place_type": kind,
                "label": f"{canonical} ({kind})"}

    def resolve_place(self, place_name: str) -> dict:
        """Resolve a place or return opaque candidates for explicit HITL."""
        try:
            selected, rows = select_place_candidate(place_name)
            ambiguous = False
        except AmbiguousPlaceError as exc:
            rows, selected, ambiguous = exc.candidates[:5], None, True
        resolution_id = uuid4().hex
        with self.lock: self.resolutions[resolution_id] = {"query": place_name, "rows": rows}
        candidates = [self._candidate(row, index) for index, row in enumerate(rows)]
        chosen = self._candidate(selected, rows.index(selected)) if selected else None
        return {"resolution_id": resolution_id, "query": place_name, "ambiguous": ambiguous,
                "candidates": candidates, "selected": chosen}

    def confirm_place(self, resolution_id: str, candidate_id: str) -> dict:
        """Confirm one retained candidate and write its canonical query cache."""
        with self.lock: resolution = self.resolutions.get(resolution_id)
        if resolution is None: raise KeyError("Unknown or expired place resolution")
        rows = resolution["rows"]
        try: index = int(candidate_id.split(":", 1)[0]); selected = rows[index]
        except (ValueError, IndexError): raise KeyError("Unknown place candidate") from None
        canonical = canonical_place_name(selected); cache_place_candidates(resolution["query"], rows, selected)
        return {**self._candidate(selected, index), "query": resolution["query"],
                "canonical_name": canonical}

    def upload_candidates(self, filename: str, content: bytes) -> dict:
        """Store one CSV under an opaque ID and never expose its backend path."""
        name = Path(filename).name
        if Path(name).suffix.lower() != ".csv": raise ValueError("Candidate upload must be a CSV")
        upload_id = uuid4().hex; path = self.upload_root / f"{upload_id}_{name}"
        path.write_bytes(content)
        with self.lock: self.uploads[upload_id] = path
        return {"upload_id": upload_id, "filename": name, "size": len(content)}

    @staticmethod
    def _mode(value: str | None) -> RoutingMode:
        """Normalize legacy router names into the explicit execution modes."""
        normalized = {None: "auto", "ors": "precise", "haversine": "quick"}.get(value, value)
        if normalized not in {"quick", "precise", "auto"}:
            raise ValueError(f"Unsupported run mode: {value}")
        return normalized  # type: ignore[return-value]

    def _internal_task(self, task: TaskSpec, candidate_upload_id: str | None) -> TaskSpec:
        """Bind an opaque upload to an internal path without persisting that path publicly."""
        data = {**task.model_dump(), "candidate_file": None}
        if candidate_upload_id:
            with self.lock:
                candidate_file = self.uploads.get(candidate_upload_id)
            if candidate_file is None:
                raise KeyError("Unknown candidate upload")
            data.update(candidate_source="csv", candidate_file=str(candidate_file))
        return TaskSpec(**data)

    def _queue_run(
        self,
        spec: RunSpec,
        candidate_upload_id: str | None,
        *,
        parent_run_id: str | None = None,
        revision: int = 0,
    ) -> dict:
        """Persist and submit one immutable run specification."""
        internal_task = self._internal_task(spec.task, candidate_upload_id)
        run_id = uuid4().hex
        public_spec = spec.model_dump(mode="json")
        public_spec["spec_hash"] = hashlib.sha256(
            json.dumps(public_spec, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.store.create(run_id, public_spec, parent_run_id=parent_run_id,
                          revision=revision, candidate_upload_id=candidate_upload_id)
        control = _RunControl(CancellationToken())
        with self.lock:
            self.controls[run_id] = control
        if self.executor is None:
            raise RuntimeError("Worker services cannot queue nested runs")
        control.future = self.executor.submit(self._run, run_id, internal_task, spec)
        return {"run_id": run_id, "status": "queued", "mode": spec.mode,
                "parent_run_id": parent_run_id, "revision": revision}

    def start_run(
        self,
        task: TaskSpec,
        mode: str = "auto",
        candidate_upload_id: str | None = None,
        *,
        router: str | None = None,
    ) -> dict:
        """Queue one confirmed run; legacy router values remain accepted."""
        resolved = self._mode(router or mode)
        public_task = TaskSpec(**{**task.model_dump(), "candidate_file": None})
        return self._queue_run(RunSpec(task=public_task, mode=resolved), candidate_upload_id)

    @staticmethod
    def _reuse_plan(old: RunSpec, new: RunSpec) -> dict[str, list[str]]:
        """Explain which content-addressed stages a revision can reuse."""
        old_task, new_task = old.task.model_dump(), new.task.model_dump()
        changed = {name for name in old_task if old_task[name] != new_task[name]}
        optimization_only = {"n_facilities", "weights", "coverage_target", "coverage_group",
                             "regional_requirements", "preference_question", "notes"}
        if old.mode == new.mode and changed <= optimization_only:
            return {"reuse": ["study", "demand", "candidates", "travel_matrix"],
                    "recompute": ["optimization", "outputs"]}
        if old.mode == new.mode and changed <= optimization_only | {"threshold_minutes"}:
            return {"reuse": ["study", "demand", "candidates", "route_pairs"],
                    "recompute": ["travel_matrix", "optimization", "outputs"]}
        if old.task.place_name == new.task.place_name:
            return {"reuse": ["study", "provider_caches"],
                    "recompute": ["demand", "candidates", "travel_matrix", "optimization", "outputs"]}
        return {"reuse": ["provider_caches"],
                "recompute": ["study", "demand", "candidates", "travel_matrix", "optimization", "outputs"]}

    def revise_run(
        self,
        run_id: str,
        task: TaskSpec | None = None,
        mode: str | None = None,
        candidate_upload_id: str | None = None,
    ) -> dict:
        """Create an immutable child run and cooperatively supersede active work."""
        old_record = self.store.get(run_id)
        old_spec = RunSpec(**old_record["spec"])
        new_task = task or old_spec.task
        new_mode = self._mode(mode or old_spec.mode)
        upload_id = candidate_upload_id or old_record["candidate_upload_id"]
        if new_task.candidate_source != "csv":
            upload_id = None
        new_spec = RunSpec(task=new_task, mode=new_mode)
        plan = self._reuse_plan(old_spec, new_spec)
        self._internal_task(new_task, upload_id)
        self.cancel_run(run_id, superseded=True)
        accepted = self._queue_run(new_spec, upload_id, parent_run_id=run_id,
                                   revision=int(old_record["revision"]) + 1)
        return {**accepted, **plan}

    def cancel_run(self, run_id: str, *, superseded: bool = False) -> dict:
        """Cancel immediately by terminating the isolated run process and its children."""
        record = self.store.get(run_id)
        terminal = {"cancelled", "superseded", "interrupted", "succeeded", "awaiting_hitl", "failed"}
        if record["status"] in terminal:
            return self.store.snapshot(run_id)
        desired = "superseded" if superseded else "cancelled"
        with self.lock:
            control = self.controls.get(run_id)
            if control:
                control.terminal_status = desired
                control.token.cancel()
                process = control.process
            else:
                process = None
        if record["status"] == "queued" and control and control.future and control.future.cancel():
            self.store.transition(run_id, ("queued",), desired, progress=None,
                                  cancel_reason="superseded" if superseded else "user")
        else:
            self.store.transition(run_id, ("queued", "running"), "cancelling",
                                  cancel_reason="superseded" if superseded else "user")
            if process is not None and process.is_alive():
                with control.termination_lock:
                    self._terminate_process(process)
                self.store.transition(
                    run_id, ("queued", "running", "cancelling"), desired,
                    progress=None, error=None,
                )
        return self.store.snapshot(run_id)

    @staticmethod
    def _terminate_process(process: multiprocessing.Process) -> None:
        """Terminate one run process tree and wait briefly for OS reclamation."""
        if process.pid is None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, check=False, timeout=5,
            )
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        process.join(timeout=2)
        if process.is_alive():
            process.kill()
            process.join(timeout=2)

    @staticmethod
    def _public(value: Any, key: str = "") -> Any:
        """Remove backend-managed filesystem paths from public payloads and events."""
        if key in {"candidate_file", "output_dir", "map", "pareto", "report", "analysis_chart"} and isinstance(value, str):
            return "<server-managed>"
        if isinstance(value, dict): return {name: OasisService._public(item, name) for name, item in value.items()}
        if isinstance(value, list): return [OasisService._public(item) for item in value]
        return value
    def _event(self, run_id: str, event: AgentEvent) -> None:
        """Append one ordered, durable public agent event."""
        payload = self._public({"kind": "agent", **event.model_dump(mode="json")})
        self.store.append_event(run_id, payload)

    def _progress(self, run_id: str, label: str, done: int, total: int, **details: Any) -> None:
        """Persist structured progress with a lightweight stage ETA."""
        now, done, total = monotonic(), int(done), int(total)
        with self.lock:
            prior = self.progress_clocks.get(run_id)
            if prior is None or prior[0] != label or done < prior[2]:
                prior = (label, now, done)
            self.progress_clocks[run_id] = prior
        elapsed = max(now - prior[1], 0.0)
        completed = max(done - prior[2], 0)
        rate = completed / elapsed if completed and elapsed else 0.0
        eta = max(total - done, 0) / rate if rate else (0.0 if done >= total else None)
        progress = {
            "kind": "progress", "label": label, "done": done, "total": total,
            "unit": details.pop("unit", "items"),
            "fraction": min(done / max(total, 1), 1.0),
            "eta_seconds": round(eta, 1) if eta is not None else None,
            "cache_hits": int(details.pop("cache_hits", details.pop("cache_pairs", 0)) or 0),
            "remote_pairs": int(details.pop("remote_pairs", details.pop("live_pairs", 0)) or 0),
            "inflight_pairs": int(details.pop("inflight_pairs", 0) or 0),
            "retry_after_seconds": details.pop("retry_after_seconds", None),
            **details,
        }
        self.store.update(run_id, progress=progress)

    @staticmethod
    def _metrics(orch: Orchestrator, payload: dict) -> dict:
        """Return compact presentation metrics without report-text scraping."""
        metrics = {}
        if orch.ctx.has("analysis"):
            solution = orch.ctx.get("analysis", {}).get("solution")
            if solution and solution.solver_status in {"Optimal", "Feasible"}:
                metrics = {"coverage": solution.covered_ratio,
                           "vulnerable": solution.vulnerable_covered_ratio,
                           "avg_walk": solution.mean_travel_minutes}
        elif orch.ctx.has("sensitivity_results"):
            baseline = orch.ctx.get("sensitivity_results", {}).get("baseline", {})
            metrics = {"coverage": baseline.get("covered_population_ratio"),
                       "vulnerable": baseline.get("vulnerable_covered_ratio"),
                       "avg_walk": baseline.get("mean_travel_minutes")}
        for item in payload.get("results", []):
            if item.get("tool") == "build_travel_matrix" and item.get("ok"):
                routing = dict(item.get("summary", {})); provenance = item.get("provenance") or {}
                routing["tier"] = provenance.get("tier", "unknown"); payload["routing_status"] = routing
        return metrics

    @staticmethod
    def _add_artifact(manifest: dict[str, Path], name: str, value: str | None,
                      allowed_root: Path) -> None:
        """Allow-list one existing artifact contained by its run directory."""
        if not value: return
        path = Path(value).resolve()
        if path.is_file() and path.is_relative_to(allowed_root.resolve()): manifest[name] = path

    def _artifacts(self, run_id: str, payload: dict, root: Path) -> dict[str, Path]:
        """Collect output, heat, and diagnostic artifacts under stable public names."""
        manifest: dict[str, Path] = {}; outputs = payload.get("outputs") or {}
        for name in ("map", "pareto", "report"): self._add_artifact(manifest, name, outputs.get(name), root)
        heat = payload.get("heat_summary") or {}
        if "map" not in manifest: self._add_artifact(manifest, "map", heat.get("map"), root)
        self._add_artifact(manifest, "heat_analysis", heat.get("analysis_chart"), root)
        if "report" not in manifest: self._add_artifact(manifest, "report", heat.get("report"), root)
        diagnostics = payload.get("diagnostics") or {}
        for handle in ("minimum_facilities", "minimum_threshold"):
            if "map" not in manifest:
                self._add_artifact(manifest, "map", (diagnostics.get(handle) or {}).get("map"), root)
        if "report" not in manifest and (payload.get("recovery") or diagnostics):
            report = root / "feasibility_diagnostic.md"; report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(render_diagnostic_report(payload.get("recovery"), diagnostics), encoding="utf-8")
            manifest["report"] = report
        return manifest

    def _phase_config(self, phase: str, root: Path) -> dict:
        """Return isolated configuration for one quick or precise phase."""
        precise = phase == "precise"
        cfg = deepcopy(self.cfg)
        cfg["output"] = {**cfg.get("output", {}), "dir": str(root)}
        cfg["accessibility"] = {
            **cfg.get("accessibility", {}),
            "router": "ors" if precise else "haversine",
            "routing_mode": phase,
            "strict_router": precise,
            "strict_routing": precise,
            "allow_fallback": not precise,
        }
        cfg["value_profiles"] = self.profiles
        cfg["max_self_correction_rounds"] = cfg.get("llm", {}).get("max_self_correction_rounds", 3)
        return cfg

    def _publish_artifacts(
        self, run_id: str, phase: str, manifest: dict[str, Path], *, preview: bool
    ) -> dict[str, str]:
        """Register private paths and return phase-stable public URLs."""
        stored = {(f"preview_{name}" if preview else name): path for name, path in manifest.items()}
        self.store.put_artifacts(run_id, phase, stored)
        return {name: f"/v1/runs/{run_id}/artifacts/"
                      f"{'preview_' if preview else ''}{name}" for name in manifest}

    def _execute_phase(
        self, run_id: str, task: TaskSpec, spec: RunSpec, phase: str, *, preview: bool
    ) -> tuple[str, dict]:
        """Execute one auditable phase and return its terminal payload."""
        root = Path(self.cfg["output"]["dir"]) / "api_runs" / run_id / phase
        cfg = self._phase_config(phase, root)
        with self.lock:
            control = self.controls[run_id]
            self.progress_clocks.pop(run_id, None)
        orch = Orchestrator(self.llm_factory(), cfg)
        orch.ctx.cancel_callback = control.token.is_cancelled
        orch.ctx.event_callback = lambda event: self._event(run_id, event)
        orch.ctx.progress_callback = (
            lambda label, done, total, **stats: self._progress(run_id, label, done, total, **stats)
        )
        orch.ctx.record("hitl", "Human-confirmed task parameters were bound before execution.",
                        place_name=task.place_name, n_facilities=task.n_facilities,
                        threshold_minutes=task.threshold_minutes,
                        weights=task.weights.model_dump() if task.weights else None,
                        candidate_source=task.candidate_source, mode=spec.mode, phase=phase,
                        mobility_scenario=task.mobility_scenario,
                        operating_window=(task.operating_window.model_dump(mode="json")
                                          if task.operating_window else None))
        internal = jsonable_encoder(orch.run(task))
        internal["task"] = spec.task.model_dump(mode="json")
        internal["metrics"] = self._metrics(orch, internal)
        routing = internal.get("routing_status") or {}
        router_used = routing.get("router")
        if phase == "precise" and routing and not str(router_used).startswith("ors"):
            raise RuntimeError(f"Precise mode requires ORS network routing; got {router_used or 'fallback'}")
        cache_keys = ("matrix_cache_hit", "cache_pairs", "live_pairs", "eligible_pairs",
                      "pruned_pairs", "planned_requests", "completed_requests")
        execution = ExecutionProvenance(
            mode=spec.mode, phase=phase,
            router_requested="ors" if phase == "precise" else "haversine",
            router_used=router_used,
            accuracy=("network" if phase == "precise" else "approximate") if routing else None,
            status="complete" if internal.get("ok") else "failed",
            parameters={"mobility_scenario": task.mobility_scenario,
                        "modes": routing.get("modes", [task.mobility_scenario]),
                        "operating_window": (task.operating_window.model_dump(mode="json")
                                             if task.operating_window else None),
                        "mode_population_share": routing.get("mode_population_share", {}),
                        "operating_hours": routing.get("operating_hours", {}),
                        "threshold_minutes": task.threshold_minutes,
                        "strict": phase == "precise"},
            cache_stats={key: routing[key] for key in cache_keys if key in routing},
        )
        internal["execution"] = execution.model_dump(mode="json")
        manifest = self._artifacts(run_id, internal, root)
        report = manifest.get("report")
        if report:
            spec_hash = self.store.get(run_id)["spec"].get("spec_hash", "unknown")
            section = (
                "\n\n## Execution Provenance\n\n"
                f"- Run specification hash: `{spec_hash}`\n"
                f"- Requested mode: {spec.mode}; completed phase: {phase}\n"
                f"- Router requested: {execution.router_requested}; router used: "
                f"{execution.router_used or 'not applicable'}\n"
                f"- Accuracy class: {execution.accuracy or 'not applicable'}\n"
                f"- Routing parameters: `{json.dumps(execution.parameters, sort_keys=True)}`\n"
                f"- Cache statistics: `{json.dumps(execution.cache_stats, sort_keys=True)}`\n"
            )
            notice = ""
            if execution.accuracy == "approximate":
                notice = (
                    "> **Approximate preview (Haversine):** This report uses estimated "
                    "straight-line travel times. Wait for ORS refinement before treating "
                    "the siting result as final.\n\n"
                )
            elif execution.accuracy == "network":
                notice = (
                    "> **Precise network result (ORS):** This report uses the completed "
                    "ORS network travel-time matrix.\n\n"
                )
            report.write_text(
                notice + report.read_text(encoding="utf-8").rstrip() + section,
                encoding="utf-8",
            )
        payload = self._public(internal)
        payload["task"] = spec.task.model_dump(mode="json")
        payload["artifacts"] = self._publish_artifacts(run_id, phase, manifest, preview=preview)
        status = "succeeded" if payload["ok"] else "awaiting_hitl" if payload.get("recovery") else "failed"
        return status, payload

    def _fail_run(self, run_id: str, spec: RunSpec, phase: str, exc: Exception) -> None:
        """Persist one backend failure without discarding an auto-mode preview."""
        recovery = {"diagnosis": str(exc),
                    "question": "Review the request and backend configuration before retrying.",
                    "suggested_changes": {}}
        payload = {"ok": False, "paused": True, "message": str(exc), "failed_tool": "backend",
                   "task": spec.task.model_dump(mode="json"), "results": [], "lineage": [],
                   "diagnostics": {}, "recovery": recovery, "metrics": {}, "artifacts": {},
                   "execution": ExecutionProvenance(
                       mode=spec.mode, phase=phase,
                       router_requested="ors" if phase == "precise" else "haversine",
                       status="failed").model_dump(mode="json")}
        root = Path(self.cfg["output"]["dir"]) / "api_runs" / run_id / phase
        root.mkdir(parents=True, exist_ok=True)
        report = root / "feasibility_diagnostic.md"
        report.write_text(render_diagnostic_report(recovery, {}), encoding="utf-8")
        preview = spec.mode == "auto" and phase == "quick"
        payload["artifacts"] = self._publish_artifacts(
            run_id, phase, {"report": report}, preview=preview
        )
        self.store.transition(run_id, ("running",), "failed", result=payload,
                              error=str(exc), progress=None)

    def _run(self, run_id: str, task: TaskSpec, spec: RunSpec) -> None:
        """Expose a cancellable wait state before serialized geospatial execution."""
        if not self.store.transition(run_id, ("queued",), "running"):
            return
        phase = "quick" if spec.mode in {"quick", "auto"} else "precise"
        self.store.update(
            run_id,
            phase=phase,
            progress={
                "kind": "progress",
                "label": "Waiting for the previous run to stop",
                "done": 0,
                "total": 1,
                "unit": "run",
                "fraction": 0.0,
                "eta_seconds": None,
                "cache_hits": 0,
                "remote_pairs": 0,
                "inflight_pairs": 0,
            },
        )
        control = self.controls[run_id]
        acquired = False
        try:
            while not acquired:
                control.token.check()
                acquired = self.execution_lock.acquire(timeout=0.05)
            control.token.check()
            if self.process_isolation:
                self._run_isolated(run_id, task, spec, control)
            else:
                self._run_serial(run_id, task, spec)
        except RunCancelled:
            self.store.transition(
                run_id,
                ("running", "cancelling"),
                control.terminal_status,
                progress=None,
                error=None,
            )
        finally:
            if acquired:
                self.execution_lock.release()
            with self.lock:
                self.controls.pop(run_id, None)
                self.progress_clocks.pop(run_id, None)

    def _run_isolated(
        self, run_id: str, task: TaskSpec, spec: RunSpec, control: _RunControl
    ) -> None:
        """Start and monitor a whole-run subprocess so cancellation is stage-independent."""
        process = multiprocessing.get_context("spawn").Process(
            target=_run_process,
            args=(self.cfg, run_id, task.model_dump(mode="json"), spec.model_dump(mode="json")),
            name=f"oasis-run-{run_id[:8]}",
        )
        process.start()
        with self.lock:
            control.process = process
        try:
            while process.is_alive():
                if control.token.cancelled():
                    with control.termination_lock:
                        self._terminate_process(process)
                    raise RunCancelled()
                process.join(timeout=0.1)
            if process.exitcode:
                if control.token.cancelled():
                    raise RunCancelled()
                record = self.store.get(run_id)
                if record["status"] in {"queued", "running", "cancelling"}:
                    raise RuntimeError(
                        f"Run worker exited unexpectedly with code {process.exitcode}"
                    )
        finally:
            with self.lock:
                control.process = None

    def _run_serial(self, run_id: str, task: TaskSpec, spec: RunSpec) -> None:
        """Run one geospatial job while preserving process-wide single-flight safety."""
        phase = "quick" if spec.mode in {"quick", "auto"} else "precise"
        try:
            with self.lock:
                control = self.controls[run_id]
            phases = ("quick", "precise") if spec.mode == "auto" else (phase,)
            for phase in phases:
                control.token.check()
                self.store.update(run_id, phase=phase, progress=None)
                status, payload = self._execute_phase(
                    run_id, task, spec, phase, preview=spec.mode == "auto" and phase == "quick"
                )
                control.token.check()
                if spec.mode == "auto" and phase == "quick":
                    self.store.update(run_id, preview_result=payload, progress=None)
                    if status == "succeeded":
                        continue
                if not self.store.transition(run_id, ("running",), status,
                                             result=payload, progress=None):
                    control.token.check()
                return
        except RunCancelled:
            with self.lock:
                control = self.controls.get(run_id)
                desired = control.terminal_status if control else "cancelled"
            self.store.transition(run_id, ("queued", "running", "cancelling"), desired,
                                  progress=None, error=None)
        except Exception as exc:
            with self.lock:
                control = self.controls.get(run_id)
            if control and control.token.cancelled():
                self.store.transition(run_id, ("queued", "running", "cancelling"),
                                      control.terminal_status, progress=None, error=None)
            else:
                self._fail_run(run_id, spec, phase, exc)

    def run_snapshot(self, run_id: str) -> dict:
        """Return one durable JSON-safe copy of current job state."""
        return self.store.snapshot(run_id)

    def artifact(self, run_id: str, name: str) -> Path:
        """Return one explicitly allow-listed run artifact."""
        path = self.store.artifact(run_id, name)
        if not path.is_file():
            raise KeyError("Unknown artifact")
        return path
