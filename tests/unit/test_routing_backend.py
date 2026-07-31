"""Provider-only ORS backend tests with no cache or block planner."""
from __future__ import annotations

import numpy as np

from oasis.core.spatial.accessibility import backend as routing_backend


def test_ors_backend_owns_payload_and_response_conversion(monkeypatch):
    """The backend converts provider payloads and seconds without persistence."""
    captured = {}

    def fake_post(url, api_key, payload, timeout_s, **kwargs):
        captured.update(
            url=url,
            api_key=api_key,
            payload=payload,
            timeout_s=timeout_s,
        )
        return {"durations": [[60.0, None]]}

    monkeypatch.setattr(routing_backend, "post_ors", fake_post)
    backend = routing_backend.OrsBackend(
        mode="walking",
        api_key="secret",
        base_url="https://ors.example/matrix/",
        timeout_s=12,
        cache_version="v2",
    )
    matrix = backend.fetch_block([[1.0, 2.0]], [[3.0, 4.0], [5.0, 6.0]])

    assert captured["url"] == "https://ors.example/matrix/foot-walking"
    assert captured["payload"]["sources"] == ["0"]
    assert captured["payload"]["destinations"] == ["1", "2"]
    assert captured["timeout_s"] == 12
    assert matrix[0, 0] == 1.0 and np.isinf(matrix[0, 1])
    assert backend.identity.profile == "foot-walking"

