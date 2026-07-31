"""Regression tests for transient Census API payload failures."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from oasis.core.data import census
from oasis.core.retry import TransientError


def test_empty_census_table_is_retryable_instead_of_index_error(monkeypatch, tmp_path):
    """An empty provider payload must never reach rows[0]."""
    monkeypatch.setattr(census, "cache_dir", lambda _: tmp_path)
    monkeypatch.setattr(
        requests,
        "get",
        lambda *args, **kwargs: SimpleNamespace(
            status_code=200,
            raise_for_status=lambda: None,
            json=lambda: [],
        ),
    )

    with pytest.raises(TransientError, match="no tract rows"):
        census._get.__wrapped__("https://census.example", {"for": "tract:*"})


def test_census_transport_error_is_retryable(monkeypatch, tmp_path):
    """SSL and connection failures must retain provider context."""
    monkeypatch.setattr(census, "cache_dir", lambda _: tmp_path)

    def fail(*args, **kwargs):
        raise requests.exceptions.SSLError("unexpected EOF")

    monkeypatch.setattr(requests, "get", fail)
    with pytest.raises(TransientError, match="Census API request failed"):
        census._get.__wrapped__("https://census.example", {"for": "tract:*"})
