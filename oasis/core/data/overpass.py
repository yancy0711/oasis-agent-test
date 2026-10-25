"""Bounded Overpass transport without hidden sleeps or recursive retries."""
import math
import logging
import os
import re
import time
from collections import OrderedDict
from threading import Lock
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
import httpx
from oasis.core.http import cancellable_request

DEFAULT_ENDPOINTS = ["https://overpass.private.coffee/api/interpreter", "https://overpass-api.de/api/interpreter"]

log = logging.getLogger(__name__)


class OverpassUnavailable(RuntimeError):
    """Provider failure, not a request to change the confirmed data source."""


# Shared by Overpass requests in this backend process.
_QUERY_LOCK = Lock()
_RATE_LIMITS = {}
_FAILURES = OrderedDict()


def payload_error(payload):
    """HTTP 200 can still contain an aborted query or an invalid response."""
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
        return "invalid Overpass response (missing elements)"
    remark = str(payload.get("remark") or "")
    if re.search(r"\berror\b|\btimed?\s*out\b|\btimeout\b|\bout of memory\b", remark, re.I):
        return f"incomplete result: {remark[:160]}"
    return None


def _rate_limit_delay(endpoint, response, headers, check):
    """Inspect status once after 429; never poll or sleep inside an API request."""
    delay = 5.0
    retry_after = response.headers.get("Retry-After", "") if response is not None else ""
    if retry_after:
        try:
            delay = max(delay, float(retry_after))
        except ValueError:
            try:
                delay = max(delay, parsedate_to_datetime(retry_after).timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                pass
    try:
        status = cancellable_request("GET", endpoint.rsplit("/", 1)[0] + "/status", headers=headers, timeout=(2, 5), check=check)
        status.raise_for_status()
        waits = [int(value) for value in re.findall(r"in (\d+) seconds", status.text)]
        if waits and not re.search(r"[1-9]\d* slots available now", status.text):
            delay = max(delay, min(waits) + 2)
    except httpx.HTTPError:
        pass
    return delay


def _fetch_payload(endpoints, query, key, timeout, check):
    if not _QUERY_LOCK.acquire(blocking=False):
        raise OverpassUnavailable("Another OpenStreetMap lookup is finishing. Stop and reset the current request, then retry.")
    try:
        now = time.monotonic()
        pause_until = _FAILURES.get(key, 0)
        if pause_until > now:
            raise OverpassUnavailable(f"The same OpenStreetMap lookup just failed. Retry in {math.ceil(pause_until-now)} seconds; your inputs are kept.")
        headers = {"User-Agent": os.getenv("OASIS_OSM_USER_AGENT", "OASIS-Agent/1.0"), "Accept": "application/json"}
        failures = []
        cooldowns = []
        # Keep the historical endpoint list in cache keys, but try the instance
        # that completed our Riverside/Stanford probes before the stalled one.
        ordered = list(reversed(endpoints)) if endpoints == DEFAULT_ENDPOINTS else endpoints
        for endpoint in ordered:
            check()
            request_headers = dict(headers)
            hosted_key = os.getenv("OVERPASS_CH_API_KEY")
            if urlparse(endpoint).hostname == "api.overpass.ch" and hosted_key:
                request_headers["X-API-Key"] = hosted_key
            remaining = _RATE_LIMITS.get(endpoint, 0) - time.monotonic()
            if remaining > 0:
                cooldowns.append(remaining)
                failures.append(f"{urlparse(endpoint).hostname}: cooling down {math.ceil(remaining)}s")
                continue
            started = time.monotonic()
            try:
                response = cancellable_request("POST", endpoint, data={"data": query}, headers=request_headers,
                                               timeout=(5, timeout), check=check)
                response.raise_for_status()
                payload = response.json()
                error = payload_error(payload)
                if error:
                    raise ValueError(error)
                # Hosted services can put credentials in the path: cache only the host.
                payload["_oasis_endpoint"] = urlparse(endpoint).hostname
                _FAILURES.pop(key, None)
                log.info("Overpass query=%s host=%s status=%s elapsed=%.1fs connect=5s read=%ss", key,
                         urlparse(endpoint).hostname, response.status_code, time.monotonic() - started, timeout)
                return payload
            except (httpx.HTTPError, ValueError) as exc:
                response = getattr(exc, "response", None)
                code = response.status_code if response is not None else type(exc).__name__
                if isinstance(exc, ValueError):
                    code = str(exc)[:180]
                elapsed = time.monotonic() - started
                failures.append(f"{urlparse(endpoint).hostname}: {code} after {elapsed:.1f}s")
                log.warning("Overpass query=%s host=%s failed=%s elapsed=%.1fs connect=5s read=%ss", key,
                            urlparse(endpoint).hostname, code, elapsed, timeout)
                if code == 429:
                    _RATE_LIMITS[endpoint] = time.monotonic() + _rate_limit_delay(endpoint, response, request_headers, check)
                    raise OverpassUnavailable(f"OpenStreetMap provider rate limit: Retry in {math.ceil(_RATE_LIMITS[endpoint]-time.monotonic())} seconds; your inputs are kept.") from exc
        if len(cooldowns) == len(endpoints):
            raise OverpassUnavailable(f"All configured OpenStreetMap providers are cooling down. Retry in {math.ceil(min(cooldowns))} seconds; your inputs are kept.")
        _FAILURES[key] = time.monotonic() + 5
        _FAILURES.move_to_end(key)
        if len(_FAILURES) > 128:
            _FAILURES.popitem(last=False)
        raise OverpassUnavailable("OpenStreetMap place lookup failed (" + ", ".join(failures) +
                           "). Your inputs are kept. Retry this search after 5 seconds or choose another location now.")
    finally:
        _QUERY_LOCK.release()
