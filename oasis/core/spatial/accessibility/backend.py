"""Provider-specific routing backends with no persistence or block planning."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import time
from threading import Lock
from typing import Any, Callable, Protocol

import numpy as np
import requests

from oasis.core.retry import RETRYABLE_STATUS, TransientError, with_retry

@dataclass(frozen=True)
class RoutingIdentity:
    """Identify results whose routing semantics are interchangeable."""

    provider: str
    profile: str
    endpoint: str
    version: str
    metric: str = "duration_minutes"

    def canonical(self) -> tuple[str, ...]:
        """Return stable identity fields for hashes and persistent pair rows."""
        return (
            self.provider,
            self.profile,
            self.endpoint.rstrip("/"),
            self.version,
            self.metric,
        )

ORS_BASE_URL = "https://api.heigit.org/openrouteservice/v2/matrix"
ORS_PROFILES = {
    "walking": "foot-walking",
    "foot-walking": "foot-walking",
    "cycling": "cycling-regular",
    "cycling-regular": "cycling-regular",
    "driving": "driving-car",
    "driving-car": "driving-car",
    "wheelchair": "wheelchair",
}


class RouterBackend(Protocol):
    """Fetch rectangular route blocks for one immutable routing identity."""

    @property
    def identity(self) -> RoutingIdentity: ...

    def fetch_block(
        self,
        sources: list[list[float]],
        destinations: list[list[float]],
        *,
        cancel_check: Callable[[], None] | None = None,
    ) -> np.ndarray: ...


def retry_after_seconds(value: str | None) -> float | None:
    """Parse Retry-After as seconds or an HTTP date."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(
                0.0,
                (retry_at - datetime.now(timezone.utc)).total_seconds(),
            )
        except (TypeError, ValueError, OverflowError):
            return None


class RequestGate:
    """Coordinate request spacing and provider cooldown across backend workers."""

    def __init__(self, interval_s: float = 0.0):
        self.interval_s = max(0.0, interval_s)
        self._lock = Lock()
        self._next_start = 0.0
        self._cooldown_until = 0.0

    def wait(self, cancel_check: Callable[[], None] | None = None) -> None:
        """Wait until one request may start, checking cancellation while idle."""
        while True:
            if cancel_check:
                cancel_check()
            with self._lock:
                now = time.monotonic()
                delay = max(self._next_start, self._cooldown_until) - now
                if delay <= 0:
                    self._next_start = now + self.interval_s
                    return
            time.sleep(min(delay, 0.25) if cancel_check else delay)

    def defer(self, seconds: float) -> None:
        """Extend the shared provider cooldown."""
        with self._lock:
            self._cooldown_until = max(
                self._cooldown_until,
                time.monotonic() + max(0.0, seconds),
            )


@with_retry(max_retries=4)
def post_ors(
    url: str,
    api_key: str,
    payload: dict[str, Any],
    timeout_s: float,
    gate: RequestGate | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """POST one ORS request and retry provider-declared transient failures."""
    if gate:
        gate.wait(cancel_check)
    elif cancel_check:
        cancel_check()
    try:
        response = requests.post(
            url,
            headers={
                "Authorization": api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout_s,
        )
    except requests.RequestException as exc:
        raise TransientError(f"ORS request failed: {exc}") from exc
    if response.status_code in RETRYABLE_STATUS:
        retry_after = retry_after_seconds(response.headers.get("Retry-After"))
        if gate and retry_after is not None:
            gate.defer(retry_after)
        raise TransientError(
            f"ORS returned HTTP {response.status_code}",
            retry_after_s=retry_after,
        )
    if not response.ok:
        detail = response.text.strip().replace("\n", " ")[:300]
        raise RuntimeError(f"ORS returned HTTP {response.status_code}: {detail}")
    result = response.json()
    if "durations" not in result:
        raise RuntimeError("ORS response contains no durations matrix")
    return result


class OrsBackend:
    """Translate rectangular matrix blocks to and from the ORS HTTP API."""

    def __init__(
        self,
        *,
        mode: str = "walking",
        api_key: str,
        base_url: str = ORS_BASE_URL,
        timeout_s: float = 60.0,
        request_interval_s: float = 0.0,
        cache_version: str = "v1",
    ):
        profile = ORS_PROFILES.get(mode)
        if profile is None:
            raise ValueError(f"unsupported ORS mode/profile: {mode}")
        if request_interval_s < 0:
            raise ValueError("request_interval_s must be non-negative")
        self.profile = profile
        self.api_key = api_key
        self.base_url = (base_url or ORS_BASE_URL).rstrip("/")
        self.timeout_s = float(timeout_s)
        self.gate = RequestGate(request_interval_s)
        self._identity = RoutingIdentity(
            provider="ors",
            profile=profile,
            endpoint=self.base_url,
            version=cache_version,
        )

    @property
    def identity(self) -> RoutingIdentity:
        """Return the cache identity for all blocks fetched by this backend."""
        return self._identity

    def fetch_block(
        self,
        sources: list[list[float]],
        destinations: list[list[float]],
        *,
        cancel_check: Callable[[], None] | None = None,
    ) -> np.ndarray:
        """Fetch one ORS duration matrix block and return minutes."""
        if not self.api_key:
            raise ValueError("ORS_API_KEY is not set")
        locations = sources + destinations
        source_count = len(sources)
        payload = {
            "locations": locations,
            "sources": [str(index) for index in range(source_count)],
            "destinations": [
                str(index) for index in range(source_count, len(locations))
            ],
            "metrics": ["duration"],
        }
        result = post_ors(
            f"{self.base_url}/{self.profile}",
            self.api_key,
            payload,
            self.timeout_s,
            gate=self.gate,
            cancel_check=cancel_check,
        )
        durations = np.asarray(
            [
                [np.inf if value is None else float(value) / 60.0 for value in row]
                for row in result["durations"]
            ],
            dtype=float,
        )
        expected = (len(sources), len(destinations))
        if durations.shape != expected:
            raise RuntimeError(
                f"ORS block shape {durations.shape} does not match {expected}"
            )
        if np.isnan(durations).any() or (durations < 0).any():
            raise RuntimeError("ORS block contains invalid travel times")
        return durations

