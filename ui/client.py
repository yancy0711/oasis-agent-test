"""Small HTTP client used by the Streamlit presentation layer."""
from __future__ import annotations
import os
from typing import Any
import httpx


class BackendError(RuntimeError):
    """Report one actionable backend HTTP or connectivity failure."""


class ApiClient:
    """Call the versioned OASIS backend without exposing domain internals."""

    def __init__(self, base_url: str | None = None, transport=None):
        """Create a reusable client for the configured backend URL."""
        url = base_url or os.getenv("OASIS_API_URL", "http://127.0.0.1:8000/v1")
        self.client = httpx.Client(base_url=url.rstrip("/"), timeout=60, transport=transport)

    def _request(self, method: str, path: str, **kwargs) -> Any:
        """Return decoded JSON or raise a concise backend error."""
        try: response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc: raise BackendError(f"Backend unavailable: {exc}") from exc
        if response.is_error:
            try: detail = response.json().get("detail", response.text)
            except ValueError: detail = response.text
            raise BackendError(f"Backend {response.status_code}: {detail}")
        return response.json()

    def config(self) -> dict:
        """Return non-secret UI defaults and value profiles."""
        return self._request("GET", "/config")

    def parse(self, prompt: str) -> dict:
        """Parse one natural-language task draft."""
        return self._request("POST", "/parse", json={"prompt": prompt})

    def resolve_place(self, place_name: str) -> dict:
        """Resolve one place query into compact candidate metadata."""
        return self._request("POST", "/places/resolve", json={"place_name": place_name})

    def confirm_place(self, resolution_id: str, candidate_id: str) -> dict:
        """Confirm one candidate without transferring its boundary geometry."""
        return self._request("POST", "/places/confirm",
                             json={"resolution_id": resolution_id, "candidate_id": candidate_id})

    def upload_candidates(self, filename: str, content: bytes) -> dict:
        """Upload one candidate CSV and return its opaque backend ID."""
        return self._request("POST", "/uploads", files={"file": (filename, content, "text/csv")})

    def start_run(self, task: dict, router: str, candidate_upload_id: str | None = None) -> dict:
        """Queue one confirmed analysis run."""
        return self._request("POST", "/runs",
                             json={"task": task, "router": router,
                                   "candidate_upload_id": candidate_upload_id})

    def get_run(self, run_id: str) -> dict:
        """Read current run status, ordered events, progress, and any result."""
        return self._request("GET", f"/runs/{run_id}")

    def artifact(self, run_id: str, name: str) -> bytes:
        """Download one allow-listed artifact as raw bytes."""
        try: response = self.client.get(f"/runs/{run_id}/artifacts/{name}", timeout=60)
        except httpx.HTTPError as exc: raise BackendError(f"Artifact unavailable: {exc}") from exc
        if response.is_error: raise BackendError(f"Artifact {name} returned {response.status_code}")
        return response.content
