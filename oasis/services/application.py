"""Application services and a bounded in-memory run queue for the HTTP API."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from fastapi.encoders import jsonable_encoder

from oasis.agent.orchestrator import LLMClient, MissingPlaceError, Orchestrator
from oasis.contracts.domain import AgentEvent, TaskSpec
from oasis.settings import PROJECT_ROOT, load_cfg
from oasis.settings import load_profiles
from oasis.core.outputs.report import render_diagnostic_report
from oasis.core.data.geocoding import (AmbiguousPlaceError, cache_place_candidates,
                                               canonical_place_name, select_place_candidate)


class OasisService:
    """Own parsing, place resolution, jobs, backend files, and agent credentials."""

    def __init__(self, cfg: dict | None = None, llm_factory=None, max_workers: int = 1):
        """Create a single-process service with one safe geospatial worker by default."""
        self.cfg = cfg or load_cfg(); self.profiles = load_profiles()
        self.llm_factory = llm_factory or (lambda: LLMClient(self.cfg["llm"]))
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="oasis")
        self.lock = Lock(); self.runs: dict[str, dict] = {}
        self.resolutions: dict[str, dict] = {}; self.uploads: dict[str, Path] = {}
        self.upload_root = PROJECT_ROOT / "tmp" / "uploads"; self.upload_root.mkdir(parents=True, exist_ok=True)

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

    def start_run(self, task: TaskSpec, router: str, candidate_upload_id: str | None = None) -> dict:
        """Queue one confirmed run and return immediately with its opaque ID."""
        public_task = task.model_dump(mode="json")
        if candidate_upload_id:
            with self.lock: candidate_file = self.uploads.get(candidate_upload_id)
            if candidate_file is None: raise KeyError("Unknown candidate upload")
            task = TaskSpec(**{**task.model_dump(), "candidate_source": "csv",
                               "candidate_file": str(candidate_file)})
        run_id = uuid4().hex
        job = {"run_id": run_id, "status": "queued", "task": public_task,
               "events": [], "progress": None, "result": None, "artifacts": {}, "error": None}
        with self.lock: self.runs[run_id] = job
        self.executor.submit(self._run, run_id, task, router, public_task)
        return {"run_id": run_id, "status": "queued"}

    @staticmethod
    def _public(value: Any, key: str = "") -> Any:
        """Remove backend-managed filesystem paths from public payloads and events."""
        if key in {"candidate_file", "output_dir", "map", "pareto", "report", "analysis_chart"} and isinstance(value, str):
            return "<server-managed>"
        if isinstance(value, dict): return {name: OasisService._public(item, name) for name, item in value.items()}
        if isinstance(value, list): return [OasisService._public(item) for item in value]
        return value
    def _event(self, run_id: str, event: AgentEvent) -> None:
        """Append one ordered public agent event to a job."""
        with self.lock:
            events = self.runs[run_id]["events"]
            events.append(self._public({"seq": len(events) + 1, "kind": "agent", **event.model_dump(mode="json")}))

    def _progress(self, run_id: str, label: str, done: int, total: int) -> None:
        """Replace one job's transient deterministic progress snapshot."""
        with self.lock: self.runs[run_id]["progress"] = {
            "kind": "progress", "label": label, "done": done, "total": total}

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

    def _run(self, run_id: str, task: TaskSpec, router: str, public_task: dict) -> None:
        """Execute one job, capturing public events, progress, result, and artifacts."""
        root = Path(self.cfg["output"]["dir"]) / "api_runs" / run_id
        cfg = deepcopy(self.cfg); cfg["output"] = {**cfg.get("output", {}), "dir": str(root)}
        cfg["accessibility"] = {**cfg.get("accessibility", {}), "router": router}
        cfg["value_profiles"] = self.profiles
        cfg["max_self_correction_rounds"] = cfg.get("llm", {}).get("max_self_correction_rounds", 3)
        with self.lock: self.runs[run_id]["status"] = "running"
        try:
            orch = Orchestrator(self.llm_factory(), cfg)
            orch.ctx.event_callback = lambda event: self._event(run_id, event)
            orch.ctx.progress_callback = lambda label, done, total: self._progress(run_id, label, done, total)
            orch.ctx.record("hitl", "Human-confirmed task parameters were bound before execution.",
                            place_name=task.place_name, n_facilities=task.n_facilities,
                            threshold_minutes=task.threshold_minutes,
                            weights=task.weights.model_dump() if task.weights else None,
                            candidate_source=task.candidate_source)
            internal = jsonable_encoder(orch.run(task)); internal["task"] = public_task
            internal["metrics"] = self._metrics(orch, internal)
            manifest = self._artifacts(run_id, internal, root)
            payload = self._public(internal); payload["task"] = public_task
            payload["artifacts"] = {name: f"/v1/runs/{run_id}/artifacts/{name}" for name in manifest}
            status = "succeeded" if payload["ok"] else "awaiting_hitl" if payload.get("recovery") else "failed"
            with self.lock:
                self.runs[run_id].update(status=status, result=payload, artifacts=manifest,
                                         progress=None)
        except Exception as exc:
            recovery = {"diagnosis": str(exc),
                        "question": "Review the request and backend configuration before retrying.",
                        "suggested_changes": {}}
            payload = {"ok": False, "paused": True, "message": str(exc), "failed_tool": "backend",
                       "task": public_task, "results": [], "lineage": [],
                       "diagnostics": {}, "recovery": recovery, "metrics": {}, "artifacts": {}}
            root.mkdir(parents=True, exist_ok=True)
            report = root / "feasibility_diagnostic.md"
            report.write_text(render_diagnostic_report(recovery, {}), encoding="utf-8")
            payload["artifacts"]["report"] = f"/v1/runs/{run_id}/artifacts/report"
            with self.lock:
                self.runs[run_id].update(status="failed", result=payload,
                                         artifacts={"report": report}, error=str(exc), progress=None)

    def run_snapshot(self, run_id: str) -> dict:
        """Return one JSON-safe copy of current job state."""
        with self.lock:
            job = self.runs.get(run_id)
            if job is None: raise KeyError("Unknown run")
            return jsonable_encoder({key: value for key, value in job.items() if key != "artifacts"})

    def artifact(self, run_id: str, name: str) -> Path:
        """Return one explicitly allow-listed run artifact."""
        with self.lock:
            job = self.runs.get(run_id); path = job and job["artifacts"].get(name)
        if not path or not path.is_file(): raise KeyError("Unknown artifact")
        return path
