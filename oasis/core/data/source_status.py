"""Read-only availability and version checks for approved public data sources."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import re
from threading import Lock
from time import monotonic
from typing import Any, Callable

import requests

_CACHE: tuple[float, dict[str, Any]] | None = None
_LOCK = Lock()


def _source(name: str, configured: str, status: str, message: str, **extra) -> dict[str, Any]:
    return {"source": name, "configured_version": configured, "status": status,
            "message": message, **extra}


def _get_json(url: str, timeout: float = 8.0) -> dict[str, Any]:
    response = requests.get(url, timeout=timeout, headers={"User-Agent": "OASIS research source monitor/1.0"})
    response.raise_for_status()
    return response.json()


def _acs_tiger(configured_year: int, get_json: Callable = _get_json) -> list[dict[str, Any]]:
    try:
        catalog = get_json("https://api.census.gov/data.json")
        years: set[int] = set()
        for dataset in catalog.get("dataset", []):
            years.update(int(value) for value in re.findall(r"/data/(20\d{2})/acs/acs5", str(dataset)))
        if not years:
            raise ValueError("ACS vintages were not identifiable")
        latest = max(years); update = latest > configured_year
        acs = _source("US Census ACS 5-year", str(configured_year),
                      "update_available" if update else "current",
                      (f"Official ACS {latest} is available; configured {configured_year} remains unchanged."
                       if update else f"Configured ACS {configured_year} matches the latest official 5-year vintage."),
                      latest_version=str(latest), official_url="https://api.census.gov/data.html")
        get_json(f"https://api.census.gov/data/{latest}/acs/acs5/variables.json")
        tiger = _source("US Census TIGER/Line tracts", str(configured_year),
                        "update_available" if update else "current",
                        (f"Review same-vintage TIGER {latest} boundaries with ACS {latest}."
                         if update else f"Configured TIGER {configured_year} is aligned with the latest ACS vintage."),
                        latest_version=str(latest),
                        official_url=f"https://www2.census.gov/geo/tiger/TIGER{latest}/TRACT/")
        return [acs, tiger]
    except Exception as exc:
        message = f"Could not verify the official Census catalog: {type(exc).__name__}."
        return [_source(name, str(configured_year), "unverified", message, official_url=url)
                for name, url in [
                    ("US Census ACS 5-year", "https://api.census.gov/data.html"),
                    ("US Census TIGER/Line tracts", f"https://www2.census.gov/geo/tiger/TIGER{configured_year}/TRACT/")]]


def _service(name: str, configured: str, url: str, get_json: Callable = _get_json) -> dict[str, Any]:
    try:
        get_json(url)
        return _source(name, configured, "available",
                       "Approved official service is reachable; no data were replaced.", official_url=url)
    except Exception as exc:
        return _source(name, configured, "unverified",
                       f"Availability could not be verified: {type(exc).__name__}.", official_url=url)


def inspect_data_sources(cfg: dict[str, Any], *, force: bool = False,
                         get_json: Callable = _get_json) -> dict[str, Any]:
    """Check approved providers, cache briefly, and never mutate data or configuration."""
    global _CACHE
    ttl = max(0.0, float(cfg.get("data", {}).get("source_monitor", {}).get("ttl_hours", 24))) * 3600
    with _LOCK:
        if not force and _CACHE and monotonic() - _CACHE[0] < ttl:
            return deepcopy(_CACHE[1])
    year = int(cfg.get("data", {}).get("census_vintage", 2022))
    jobs = [
        lambda: _acs_tiger(year, get_json),
        lambda: [_service("Landsat Collection 2 Level-2 via Planetary Computer",
                          "Collection 2 / configured analysis period",
                          "https://planetarycomputer.microsoft.com/api/stac/v1/collections/landsat-c2-l2", get_json)],
        lambda: [_service("OpenStreetMap Nominatim", "live service",
                          "https://nominatim.openstreetmap.org/status.php?format=json", get_json)],
        lambda: [_source("openrouteservice", "live API", "unverified",
                         "Check the official status page and test a route with the configured key; no unauthenticated routing health check was performed.",
                         official_url="https://status.heigit.org/")],
    ]
    sources: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="source-status") as pool:
        for items in pool.map(lambda job: job(), jobs):
            sources.extend(items)
    updates = any(item["status"] == "update_available" for item in sources)
    warnings = ([
        "Review ACS attributes and TIGER geometries as one same-vintage update; never combine new ACS data with old tract boundaries.",
        "Before accepting an update, rerun field mappings, tract joins, population aggregation, missingness, travel matrices, and vulnerability sensitivity; also confirm whether the Landsat study period should change.",
    ] if updates else [])
    result = {"checked_at": datetime.now(timezone.utc).isoformat(),
              "policy": "notify_only_no_automatic_replacement", "updates_available": updates,
              "sources": sources, "compatibility_warnings": warnings}
    with _LOCK:
        _CACHE = (monotonic(), deepcopy(result))
    return result
