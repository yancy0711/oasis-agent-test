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

    def parse(self, prompt: str, llm_session_id: str | None = None) -> dict:
        """Return a parsed draft without calling an LLM."""
        self.calls.append(("parse", prompt))
        return {"disposition": "full_analysis",
                "task": {"place_name": "Phoenix, Arizona, USA"}, "missing_place": False,
                "message": "", "reason": "Supported full analysis.", "alternative": "",
                "events": []}

    def official_resources(self, place_name: str) -> dict:
        self.calls.append(("official_resources", place_name))
        return {"checked_at": "2026-01-01T00:00:00+00:00", "place_name": place_name,
                "place_type": "city", "status": "found", "message": "One match found.",
                "resources": [{"title": "Phoenix heat relief", "publisher": "City of Phoenix",
                               "url": "https://catalog.data.gov/dataset/phoenix-heat-relief",
                               "verification": "Verify current dates and hours."}]}

    def test_llm_connection(self, provider, base_url, model, api_key, *, allow_private=False):
        self.calls.append(("llm_test", provider, base_url, model, "<redacted>", allow_private))
        return {"session_id": "session-1", "provider": provider, "model": model,
                "resolved_model": model,
                "latency_ms": 12, "expires_in_seconds": 7200, "structured_output": True}

    def list_llm_models(self, provider, base_url, api_key, *, allow_private=False):
        self.calls.append(("llm_models", provider, base_url, "<redacted>", allow_private))
        return {"provider": provider, "models": ["qwen2.5:latest", "llama3.2:latest"]}

    def revoke_llm_session(self, session_id):
        self.calls.append(("llm_revoke", session_id))
        return session_id == "session-1"

    def data_source_status(self, force: bool = False) -> dict:
        self.calls.append(("source_status", force))
        return {"checked_at": "2026-01-01T00:00:00+00:00",
                "policy": "notify_only_no_automatic_replacement",
                "updates_available": False,
                "sources": [{"source": "ACS", "configured_version": "2022",
                             "latest_version": "2022", "status": "current", "message": "Current"}],
                "compatibility_warnings": []}

    def resolve_place(self, place_name: str) -> dict:
        """Return one opaque place candidate without geocoding."""
        self.calls.append(("resolve", place_name))
        candidate = {"candidate_id": "candidate-1", "canonical_name": "Phoenix, Arizona, USA",
                     "display_name": "Phoenix, Maricopa County, Arizona, USA",
                     "place_type": "city", "label": "Phoenix (city)"}
        return {"resolution_id": "resolution-1", "query": place_name, "ambiguous": False,
                "candidates": [candidate], "selected": candidate}

    def confirm_place(self, resolution_id: str, candidate_id: str) -> dict:
        """Confirm only the retained fake candidate."""
        self.calls.append(("confirm", resolution_id, candidate_id))
        if candidate_id != "candidate-1": raise KeyError("Unknown place candidate")
        return {"candidate_id": candidate_id, "canonical_name": "Phoenix, Arizona, USA",
                "display_name": "Phoenix, Maricopa County, Arizona, USA",
                "place_type": "city", "label": "Phoenix (city)", "query": "Phoenix"}

    def upload_candidates(self, filename: str, content: bytes) -> dict:
        """Return an opaque upload ID while retaining no file."""
        self.calls.append(("upload", filename, content))
        if not filename.endswith(".csv"): raise ValueError("Candidate upload must be a CSV")
        return {"upload_id": "upload-1", "filename": filename, "size": len(content)}

    def start_run(self, task, router: str, candidate_upload_id: str | None,
                  *, llm_session_id: str | None = None) -> dict:
        """Accept a validated TaskSpec and return a queued run."""
        self.calls.append(("start", task, router, candidate_upload_id))
        if candidate_upload_id == "missing": raise KeyError("Unknown candidate upload")
        return {"run_id": "run-1", "status": "queued"}

    def run_snapshot(self, run_id: str) -> dict:
        """Return one terminal snapshot for the known run."""
        self.calls.append(("status", run_id))
        if run_id != "run-1": raise KeyError("Unknown run")
        task = TaskSpec(place_name="Phoenix, Arizona, USA").model_dump(mode="json")
        return {"run_id": run_id, "parent_run_id": None, "revision": 0,
                "status": "succeeded", "phase": "quick", "mode": "quick",
                "spec": {"task": task, "mode": "quick"}, "task": task,
                "events": [], "progress": None, "preview_result": None,
                "result": {"ok": True, "task": task,
                           "execution": {"mode": "quick", "phase": "quick",
                                         "router_requested": "haversine", "status": "complete"},
                           "artifacts": {"report": "/v1/runs/run-1/artifacts/report"}},
                "error": None, "cancel_reason": None,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:01+00:00"}

    def cancel_run(self, run_id: str) -> dict:
        """Return an idempotent cancelled snapshot for the known run."""
        self.calls.append(("cancel", run_id))
        if run_id != "run-1": raise KeyError("Unknown run")
        snapshot = self.run_snapshot(run_id)
        return {**snapshot, "status": "cancelled", "result": None}

    def revise_run(self, run_id: str, task, mode: str | None, candidate_upload_id: str | None,
                   *, llm_session_id: str | None = None) -> dict:
        """Return one queued immutable child run."""
        self.calls.append(("revise", run_id, task, mode, candidate_upload_id))
        if run_id != "run-1": raise KeyError("Unknown run")
        return {"run_id": "run-2", "status": "queued", "mode": mode or "precise",
                "parent_run_id": run_id, "revision": 1,
                "reuse": ["travel_matrix"], "recompute": ["optimization"]}

    def plan_point_route(self, run_id: str, longitude: float, latitude: float, mode: str) -> dict:
        self.calls.append(("route", run_id, longitude, latitude, mode))
        if run_id != "run-1": raise KeyError("Unknown run")
        return {"type": "FeatureCollection", "features": [],
                "summary": {"destination_id": "site-1", "duration_minutes": 7.5}}

    def plan_place_route(self, run_id: str, query: str, mode: str) -> dict:
        self.calls.append(("place_route", run_id, query, mode))
        if run_id != "run-1": raise KeyError("Unknown run")
        return {"type": "FeatureCollection", "features": [],
                "summary": {"destination_id": "site-1", "duration_minutes": 7.5,
                            "origin_label": query}}

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
    assert config["agent"] == {"max_steps": 14, "provider": "openrouter",
                               "model": "", "base_url": ""}
    assert config["execution"]["modes"] == ["auto", "quick", "precise"]
    assert config["execution"]["default_mode"] == "auto"
    response = client.post("/v1/parse", json={"prompt": "Site centers in Phoenix"})
    assert response.status_code == 200
    assert response.json()["task"]["place_name"] == "Phoenix, Arizona, USA"
    assert service.calls[-1] == ("parse", "Site centers in Phoenix")


def test_data_source_status_is_notify_only(tmp_path):
    client, service = _client(tmp_path)
    response = client.get("/v1/data-sources/status")
    assert response.status_code == 200
    assert response.json()["policy"] == "notify_only_no_automatic_replacement"
    assert service.calls[-1] == ("source_status", False)


def test_official_resources_are_links_not_candidate_replacements(tmp_path):
    client, service = _client(tmp_path)
    response = client.get("/v1/official-resources", params={"place_name": "Phoenix, Arizona, USA"})
    assert response.status_code == 200
    assert response.json()["resources"][0]["publisher"] == "City of Phoenix"
    assert service.calls[-1] == ("official_resources", "Phoenix, Arizona, USA")


def test_streetview_endpoint_degrades_without_a_server_token(tmp_path, monkeypatch):
    monkeypatch.delenv("MAPILLARY_ACCESS_TOKEN", raising=False)
    client, _ = _client(tmp_path)
    response = client.get("/streetview", params={"lat": 40.75, "lon": -73.98})
    assert response.status_code == 200
    assert response.json()["status"] == "not_configured"
    assert response.json()["available"] is False


def test_byok_connection_returns_only_an_opaque_session(tmp_path):
    client, service = _client(tmp_path)
    response = client.post("/v1/llm/test", json={
        "provider": "openrouter", "base_url": "https://openrouter.ai/api/v1",
        "model": "openai/gpt-4o", "api_key": "visitor-secret",
    })
    assert response.status_code == 200
    assert response.json()["session_id"] == "session-1"
    assert "visitor-secret" not in response.text
    assert service.calls[-1][4] == "<redacted>"
    revoked = client.delete("/v1/llm/sessions/session-1")
    assert revoked.status_code == 200 and revoked.json() == {"revoked": True}
    assert service.calls[-1] == ("llm_revoke", "session-1")


def test_model_discovery_does_not_create_a_session(tmp_path):
    client, service = _client(tmp_path)
    response = client.post("/v1/llm/models", json={
        "provider": "local", "base_url": "http://127.0.0.1:11434/v1", "api_key": "",
    })
    assert response.status_code == 200
    assert response.json()["models"] == ["qwen2.5:latest", "llama3.2:latest"]
    assert service.calls[-1][0] == "llm_models"


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


def test_point_route_contract(tmp_path):
    client, service = _client(tmp_path)
    response = client.post("/v1/runs/run-1/routes", json={
        "longitude": -112.07, "latitude": 33.45, "mode": "walking"
    })
    assert response.status_code == 200
    assert response.json()["summary"]["destination_id"] == "site-1"
    assert service.calls[-1] == ("route", "run-1", -112.07, 33.45, "walking")


def test_place_route_contract(tmp_path):
    client, service = _client(tmp_path)
    response = client.post("/v1/runs/run-1/routes/from-place", json={
        "query": "Phoenix City Hall", "mode": "walking"
    })
    assert response.status_code == 200
    assert response.json()["summary"]["origin_label"] == "Phoenix City Hall"
    assert service.calls[-1] == ("place_route", "run-1", "Phoenix City Hall", "walking")


def test_artifact_allow_list_and_route_errors(tmp_path):
    """Artifacts must be served only through the service allow-list."""
    client, _ = _client(tmp_path)
    response = client.get("/v1/runs/run-1/artifacts/report")
    assert response.status_code == 200 and response.content == b"# Result"
    assert response.headers["content-disposition"].startswith("inline;")
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
    paths = {"map": root / "heat.geojson", "analysis_chart": root / "heat.png", "report": root / "heat.md"}
    for path in paths.values(): path.write_text("heat", encoding="utf-8")
    service = object.__new__(OasisService)
    manifest = service._artifacts("run-1", {"heat_summary": {key: str(value) for key, value in paths.items()}}, root)
    assert set(manifest) == {"map", "heat_analysis", "report"}
    assert all(path.is_relative_to(root) for path in manifest.values())
