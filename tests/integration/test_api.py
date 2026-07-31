"""Offline route-contract tests for the FastAPI presentation boundary."""
from pathlib import Path
import time

from fastapi.testclient import TestClient

from api.main import create_app
from oasis.services.application import OasisService
from oasis.contracts.domain import AgentEvent, TaskSpec


class FakeService:
    """Implement the small service surface used by the HTTP routes."""

    def __init__(self, artifact: Path):
        """Store deterministic responses and one allow-listed artifact."""
        self.cfg = {"optimization": {"n_facilities": 8},
                    "accessibility": {"threshold_minutes": 15, "ors_base_url": "secret"}}
        self.profiles = {"balanced": {"label": "Balanced"}}
        self.allowed_artifact = artifact
        self.calls = []

    def parse(self, prompt: str) -> dict:
        """Return a parsed draft without calling an LLM."""
        self.calls.append(("parse", prompt))
        return {"task": {"place_name": "Phoenix, Arizona, USA"}, "missing_place": False,
                "message": "", "events": []}

    def resolve_place(self, place_name: str) -> dict:
        """Return one opaque place candidate without geocoding."""
        self.calls.append(("resolve", place_name))
        return {"resolution_id": "resolution-1", "query": place_name, "ambiguous": False,
                "candidates": [{"candidate_id": "candidate-1", "label": "Phoenix (city)"}],
                "selected": {"candidate_id": "candidate-1", "label": "Phoenix (city)"}}

    def confirm_place(self, resolution_id: str, candidate_id: str) -> dict:
        """Confirm only the retained fake candidate."""
        self.calls.append(("confirm", resolution_id, candidate_id))
        if candidate_id != "candidate-1": raise KeyError("Unknown place candidate")
        return {"candidate_id": candidate_id, "canonical_name": "Phoenix, Arizona, USA"}

    def upload_candidates(self, filename: str, content: bytes) -> dict:
        """Return an opaque upload ID while retaining no file."""
        self.calls.append(("upload", filename, content))
        if not filename.endswith(".csv"): raise ValueError("Candidate upload must be a CSV")
        return {"upload_id": "upload-1", "filename": filename, "size": len(content)}

    def start_run(self, task, router: str, candidate_upload_id: str | None) -> dict:
        """Accept a validated TaskSpec and return a queued run."""
        self.calls.append(("start", task, router, candidate_upload_id))
        if candidate_upload_id == "missing": raise KeyError("Unknown candidate upload")
        return {"run_id": "run-1", "status": "queued"}

    def run_snapshot(self, run_id: str) -> dict:
        """Return one terminal snapshot for the known run."""
        self.calls.append(("status", run_id))
        if run_id != "run-1": raise KeyError("Unknown run")
        return {"run_id": run_id, "status": "succeeded", "events": [], "progress": None,
                "result": {"ok": True, "artifacts": {"report": "/v1/runs/run-1/artifacts/report"}}}

    def cancel_run(self, run_id: str) -> dict:
        """Return an idempotent cancelled snapshot for the known run."""
        self.calls.append(("cancel", run_id))
        if run_id != "run-1": raise KeyError("Unknown run")
        return {"run_id": run_id, "status": "cancelled"}

    def revise_run(self, run_id: str, task, mode: str | None, candidate_upload_id: str | None) -> dict:
        """Return one queued immutable child run."""
        self.calls.append(("revise", run_id, task, mode, candidate_upload_id))
        if run_id != "run-1": raise KeyError("Unknown run")
        return {"run_id": "run-2", "status": "queued", "mode": mode or "precise",
                "parent_run_id": run_id, "revision": 1,
                "reuse": ["travel_matrix"], "recompute": ["optimization"]}

    def artifact(self, run_id: str, name: str) -> Path:
        """Expose only the explicitly allow-listed report."""
        self.calls.append(("artifact", run_id, name))
        if run_id != "run-1" or name != "report": raise KeyError("Unknown artifact")
        return self.allowed_artifact


def _client(tmp_path: Path) -> tuple[TestClient, FakeService]:
    """Create one isolated API client and fake backend service."""
    report = tmp_path / "report.md"; report.write_text("# Result", encoding="utf-8")
    service = FakeService(report)
    return TestClient(create_app(service)), service


def test_health_config_and_parse_contract(tmp_path):
    """Health and parsing must expose public data without leaking provider settings."""
    client, service = _client(tmp_path)
    assert client.get("/health").json() == {"status": "ok"}
    config = client.get("/v1/config").json()
    assert config["optimization"]["n_facilities"] == 8
    assert config["accessibility"] == {"threshold_minutes": 15}
    assert config["agent"] == {"max_steps": 14}
    assert config["execution"]["modes"] == ["auto", "quick", "precise"]
    assert config["execution"]["default_mode"] == "auto"
    response = client.post("/v1/parse", json={"prompt": "Site centers in Phoenix"})
    assert response.status_code == 200
    assert response.json()["task"]["place_name"] == "Phoenix, Arizona, USA"
    assert service.calls[-1] == ("parse", "Site centers in Phoenix")


def test_place_upload_run_and_status_contract(tmp_path):
    """Place HITL, uploads, run submission, and polling must preserve opaque IDs."""
    client, service = _client(tmp_path)
    resolved = client.post("/v1/places/resolve", json={"place_name": "Phoenix"}).json()
    assert resolved["resolution_id"] == "resolution-1"
    confirmed = client.post("/v1/places/confirm", json={
        "resolution_id": resolved["resolution_id"], "candidate_id": "candidate-1"}).json()
    assert confirmed["canonical_name"] == "Phoenix, Arizona, USA"

    upload = client.post("/v1/uploads", files={
        "file": ("sites.csv", b"name,lat,lon\nA,1,2\n", "text/csv")})
    assert upload.status_code == 200 and upload.json()["upload_id"] == "upload-1"
    started = client.post("/v1/runs", json={
        "task": {"place_name": "Phoenix, Arizona, USA"},
        "router": "haversine", "candidate_upload_id": "upload-1"})
    assert started.status_code == 202 and started.json() == {"run_id": "run-1", "status": "queued"}
    start_call = next(call for call in service.calls if call[0] == "start")
    assert start_call[1].place_name == "Phoenix, Arizona, USA"
    assert start_call[2:] == ("quick", "upload-1")
    assert client.get("/v1/runs/run-1").json()["status"] == "succeeded"


def test_cancel_and_revision_contracts(tmp_path):
    """Cancellation is explicit and revisions preserve immutable parent identity."""
    client, service = _client(tmp_path)
    cancelled = client.post("/v1/runs/run-1/cancel")
    assert cancelled.status_code == 202 and cancelled.json()["status"] == "cancelled"
    revised = client.post("/v1/runs/run-1/revisions", json={
        "task": {"place_name": "Phoenix", "n_facilities": 4}, "mode": "quick"
    })
    assert revised.status_code == 202
    assert revised.json()["parent_run_id"] == "run-1" and revised.json()["revision"] == 1
    assert service.calls[-1][0:2] == ("revise", "run-1")
    assert service.calls[-1][2].n_facilities == 4 and service.calls[-1][3] == "quick"


def test_artifact_allow_list_and_route_errors(tmp_path):
    """Artifacts must be served only through the service allow-list."""
    client, _ = _client(tmp_path)
    response = client.get("/v1/runs/run-1/artifacts/report")
    assert response.status_code == 200 and response.content == b"# Result"
    assert response.headers["content-disposition"].endswith('filename="report.md"')
    assert client.get("/v1/runs/run-1/artifacts/secret").status_code == 404
    assert client.get("/v1/runs/missing").status_code == 404
    assert client.post("/v1/places/confirm", json={
        "resolution_id": "resolution-1", "candidate_id": "missing"}).status_code == 404
    assert client.post("/v1/runs", json={
        "task": {"place_name": "Phoenix"}, "candidate_upload_id": "missing"}).status_code == 404

def test_service_redacts_backend_paths():
    """Public payloads must not reveal backend-managed filesystem paths."""
    value = {"candidate_file": "C:/secret/sites.csv", "outputs": {"map": "C:/secret/map.html"},
             "safe": "visible"}
    assert OasisService._public(value) == {
        "candidate_file": "<server-managed>", "outputs": {"map": "<server-managed>"}, "safe": "visible"}


def test_service_artifact_manifest_rejects_paths_outside_run(tmp_path):
    """Artifact manifests must retain only files contained by one run root."""
    run_root = tmp_path / "run"; run_root.mkdir()
    inside = run_root / "report.md"; inside.write_text("inside", encoding="utf-8")
    outside = tmp_path / "secret.md"; outside.write_text("outside", encoding="utf-8")
    manifest = {}
    OasisService._add_artifact(manifest, "report", str(inside), run_root)
    OasisService._add_artifact(manifest, "secret", str(outside), run_root)
    assert manifest == {"report": inside.resolve()}

class _FakeContext:
    """Expose the Context callbacks used by the HTTP run service."""

    def __init__(self):
        """Initialize callback slots without domain handles."""
        self.event_callback = None; self.progress_callback = None

    def record(self, stage: str, message: str, tool: str | None = None, **data) -> None:
        """Forward one typed event to the service callback."""
        if self.event_callback: self.event_callback(AgentEvent(stage=stage, message=message, tool=tool, data=data))

    def has(self, name: str) -> bool:
        """Report no optional analysis handle for this transport test."""
        return False


class _FakeOrchestrator:
    """Return one successful result without invoking an LLM or geospatial tool."""

    def __init__(self, llm, cfg):
        """Retain only the callback-compatible fake context."""
        self.ctx = _FakeContext()

    def run(self, task: TaskSpec) -> dict:
        """Emit progress and return a minimal successful agent result."""
        self.ctx.progress_callback("fake work", 1, 1)
        return {"ok": True, "paused": False, "message": "done", "failed_tool": None,
                "results": [], "lineage": [], "outputs": None, "heat_summary": None,
                "diagnostics": {}, "narrative": None, "recovery": None, "history": []}


def test_service_runs_jobs_and_exposes_public_status(tmp_path, monkeypatch):
    """The real service queue must transition to a terminal public snapshot."""
    monkeypatch.setattr("oasis.services.application.Orchestrator", _FakeOrchestrator)
    cfg = {"llm": {}, "accessibility": {}, "optimization": {}, "output": {"dir": str(tmp_path)}}
    service = OasisService(cfg=cfg, llm_factory=lambda: object())
    try:
        run_id = service.start_run(TaskSpec(place_name="Phoenix"), "haversine")["run_id"]
        deadline = time.monotonic() + 2
        while (snapshot := service.run_snapshot(run_id))["status"] not in {"succeeded", "failed"}:
            assert time.monotonic() < deadline; time.sleep(.01)
        assert snapshot["status"] == "succeeded"
        assert snapshot["result"]["task"]["place_name"] == "Phoenix"
        assert snapshot["events"][0]["stage"] == "hitl"
    finally:
        service.executor.shutdown(wait=True)

def test_service_collects_allow_listed_heat_artifacts(tmp_path):
    """Heat review artifacts must remain inside the run root and use stable public names."""
    root = tmp_path / "run"; root.mkdir()
    paths = {"map": root / "heat.html", "analysis_chart": root / "heat.png", "report": root / "heat.md"}
    for path in paths.values(): path.write_text("heat", encoding="utf-8")
    service = object.__new__(OasisService)
    manifest = service._artifacts("run-1", {"heat_summary": {key: str(value) for key, value in paths.items()}}, root)
    assert set(manifest) == {"map", "heat_analysis", "report"}
    assert all(path.is_relative_to(root) for path in manifest.values())