"""Offline tests for graceful Mapillary street-level imagery lookup."""
import json

from oasis.core.data.streetview import nearest_streetview


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_streetview_degrades_when_token_is_not_configured(monkeypatch):
    monkeypatch.delenv("MAPILLARY_ACCESS_TOKEN", raising=False)
    result = nearest_streetview(40.75, -73.98)
    assert not result["available"]
    assert result["status"] == "not_configured"


def test_streetview_selects_nearest_image_inside_radius(monkeypatch):
    monkeypatch.setenv("MAPILLARY_ACCESS_TOKEN", "test-token")
    payload = {"data": [
        {"id": "far", "computed_geometry": {"coordinates": [-73.9807, 40.7500]}},
        {"id": "near", "computed_geometry": {"coordinates": [-73.9801, 40.7500]}},
    ]}
    responses = iter([
        _Response(payload),
        _Response({"id": "near", "thumb_1024_url": "https://example.test/near.jpg",
                   "captured_at": "2025-06-01T00:00:00Z", "creator": {"username": "mapper"}}),
    ])
    monkeypatch.setattr("oasis.core.data.streetview.urlopen", lambda *_args, **_kwargs: next(responses))
    result = nearest_streetview(40.75, -73.98, 50)
    assert result["available"] and result["image_id"] == "near"
    assert result["creator"] == "mapper"
    assert result["distance_m"] < 20


def test_streetview_prefers_a_panorama_within_the_radius(monkeypatch):
    monkeypatch.setenv("MAPILLARY_ACCESS_TOKEN", "test-token")
    responses = iter([
        _Response({"data": [
            {"id": "near", "is_pano": False,
             "computed_geometry": {"coordinates": [-73.9802, 40.7500]}},
            {"id": "pano", "is_pano": True,
             "computed_geometry": {"coordinates": [-73.9802, 40.7500]}},
        ]}),
        _Response({"id": "pano", "thumb_1024_url": "https://example.test/pano.jpg"}),
    ])
    monkeypatch.setattr("oasis.core.data.streetview.urlopen", lambda *_args, **_kwargs: next(responses))
    result = nearest_streetview(40.75, -73.98, 50)
    assert result["available"] and result["image_id"] == "pano"


def test_streetview_retries_one_transient_timeout(monkeypatch):
    monkeypatch.setenv("MAPILLARY_ACCESS_TOKEN", "test-token")
    responses = iter([
        TimeoutError(),
        _Response({"data": [
            {"id": "near", "computed_geometry": {"coordinates": [-73.9801, 40.7500]}},
        ]}),
        _Response({"id": "near", "thumb_1024_url": "https://example.test/near.jpg"}),
    ])
    calls = []

    def _urlopen(*_args, **_kwargs):
        calls.append(True)
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr("oasis.core.data.streetview.urlopen", _urlopen)
    result = nearest_streetview(40.75, -73.98, 100)
    assert result["available"] and result["image_id"] == "near"
    assert len(calls) == 3
