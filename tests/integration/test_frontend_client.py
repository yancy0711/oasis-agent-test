"""Offline transport tests for the Streamlit HTTP client."""
import httpx
import pytest

from ui.client import ApiClient, BackendError


def test_client_serializes_contracts_and_downloads_artifacts():
    """Every public client method must use the versioned API contract."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Capture requests and return endpoint-specific fake responses."""
        seen.append(request)
        if request.url.path.endswith("/artifacts/report"):
            return httpx.Response(200, content=b"# Report")
        return httpx.Response(200, json={"path": request.url.path})

    client = ApiClient("https://api.example/v1", transport=httpx.MockTransport(handler))
    try:
        assert client.parse("Site centers")["path"] == "/v1/parse"
        assert client.config()["path"] == "/v1/config"
        client.resolve_place("Phoenix")
        client.confirm_place("resolution-1", "candidate-1")
        client.upload_candidates("sites.csv", b"name\nA\n")
        client.start_run({"place_name": "Phoenix"}, "ors", "upload-1")
        client.get_run("run-1")
        client.cancel_run("run-1")
        client.revise_run("run-1", {"place_name": "Phoenix", "n_facilities": 4}, "quick")
        assert client.artifact("run-1", "report") == b"# Report"
    finally:
        client.client.close()

    assert [request.url.path for request in seen] == [
        "/v1/parse", "/v1/config", "/v1/places/resolve", "/v1/places/confirm", "/v1/uploads",
        "/v1/runs", "/v1/runs/run-1", "/v1/runs/run-1/cancel",
        "/v1/runs/run-1/revisions", "/v1/runs/run-1/artifacts/report"]
    assert seen[0].read() == b'{"prompt":"Site centers"}'
    assert b'name="file"; filename="sites.csv"' in seen[4].read()
    assert b'"candidate_upload_id":"upload-1"' in seen[5].read()
    assert b'"mode":"quick"' in seen[8].read()


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(422, json={"detail": "invalid place"}), "Backend 422: invalid place"),
        (httpx.Response(500, text="service failed"), "Backend 500: service failed"),
    ],
)
def test_client_maps_http_errors_to_actionable_backend_errors(response, message):
    """JSON and plain-text failures must become concise presentation errors."""
    client = ApiClient("https://api.example/v1",
                       transport=httpx.MockTransport(lambda request: response))
    try:
        with pytest.raises(BackendError, match=message): client.parse("bad request")
    finally:
        client.client.close()


def test_client_maps_connectivity_and_artifact_errors():
    """Transport and artifact failures must not leak raw HTTPX exceptions."""

    def unavailable(request: httpx.Request) -> httpx.Response:
        """Simulate an unreachable backend."""
        raise httpx.ConnectError("connection refused", request=request)

    client = ApiClient("https://api.example/v1", transport=httpx.MockTransport(unavailable))
    try:
        with pytest.raises(BackendError, match="Backend unavailable: connection refused"):
            client.get_run("run-1")
        with pytest.raises(BackendError, match="Artifact unavailable: connection refused"):
            client.artifact("run-1", "report")
    finally:
        client.client.close()


def test_client_reports_artifact_status_without_decoding_json():
    """Binary artifact errors must report their name and status code."""
    client = ApiClient("https://api.example/v1",
                       transport=httpx.MockTransport(lambda request: httpx.Response(404)))
    try:
        with pytest.raises(BackendError, match="Artifact map returned 404"):
            client.artifact("run-1", "map")
    finally:
        client.client.close()
