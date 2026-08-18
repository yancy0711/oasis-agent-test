"""Notify-only discovery of city and county cooling-resource catalogs."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re
from threading import Lock
from time import monotonic
from typing import Any, Callable

import requests

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_LOCK = Lock()


def _catalog_search(query: str, timeout: float = 10.0) -> dict[str, Any]:
    response = requests.get(
        "https://catalog.data.gov/api/3/action/package_search",
        params={"q": query, "rows": 10}, timeout=timeout,
        headers={"User-Agent": "OASIS official resource discovery/1.0"},
    )
    response.raise_for_status()
    return response.json()


def _area(place_name: str) -> tuple[str, str]:
    parts = [part.strip() for part in place_name.split(",") if part.strip()]
    parts = [part for part in parts if part.lower() not in {"usa", "united states"}]
    if len(parts) < 2:
        return "state", parts[0] if parts else place_name
    return ("county" if "county" in parts[0].lower() else "city"), parts[0]


def discover_official_resources(
    place_name: str, *, search: Callable[[str], dict[str, Any]] = _catalog_search,
    ttl_hours: float = 24,
) -> dict[str, Any]:
    """Find relevant official catalog entries without replacing candidate data."""
    place_type, locality = _area(place_name)
    checked = datetime.now(timezone.utc).isoformat()
    if place_type == "state":
        return {"checked_at": checked, "place_name": place_name, "place_type": place_type,
                "status": "not_applicable", "resources": [],
                "message": "Official resource discovery is limited to city and county study areas."}
    key = place_name.casefold()
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and monotonic() - cached[0] < ttl_hours * 3600:
            return deepcopy(cached[1])
    try:
        payload = search(f'"{locality}" ("cooling center" OR "heat relief")')
        packages = payload.get("result", {}).get("results", [])
        locality_token = re.sub(r"\s+(city|county)$", "", locality, flags=re.I).casefold()
        matches = []
        for item in packages:
            text = " ".join(str(item.get(field, "")) for field in ("title", "notes", "organization")).casefold()
            if locality_token not in text or not re.search(r"cooling|heat relief|heat respite", text):
                continue
            slug = str(item.get("name", "")).strip()
            matches.append({
                "title": str(item.get("title") or slug).strip(),
                "publisher": str((item.get("organization") or {}).get("title", "Data.gov catalog")).strip(),
                "url": f"https://catalog.data.gov/dataset/{slug}" if slug else "https://catalog.data.gov/dataset",
                "verification": "Official catalog match; verify current dates, hours, and facility status.",
            })
        result = {"checked_at": checked, "place_name": place_name, "place_type": place_type,
                  "status": "found" if matches else "not_found", "resources": matches[:3],
                  "message": ("Official catalog entries were found for user verification." if matches else
                              "No matching official city or county cooling-resource catalog was confirmed automatically.")}
    except Exception as exc:
        result = {"checked_at": checked, "place_name": place_name, "place_type": place_type,
                  "status": "unverified", "resources": [],
                  "message": f"Official catalog discovery could not be completed: {type(exc).__name__}."}
    with _LOCK:
        _CACHE[key] = (monotonic(), deepcopy(result))
    return result
