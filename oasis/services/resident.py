"""Nearby-place discovery and point routing, independent of optimizer runs."""
import hashlib
import json
import math
import os
from copy import deepcopy
from contextlib import contextmanager
from threading import Lock, Event
from time import monotonic, time
from uuid import uuid4

import httpx
from oasis.contracts.resident import ResidentOrigin, ResidentSearchRequest, ResidentSearchResponse
from oasis.core.cache import CACHE_DIR, atomic_write_text
from oasis.core.cancellation import CancellationToken, RunCancelled
from oasis.core.http import cancellable_request
from oasis.core.data.geocoding import reverse_origin_address, search_origin_candidates
from oasis.core.data.resident_places import fetch_nearby_places
from oasis.core.spatial.accessibility.backend import ORS_PROFILES, RequestGate
from oasis.core.spatial.routes import _directions_url


def route_green_score(feature):
    """Distance-weighted ORS green index; require nearly complete coverage."""
    properties = feature.get("properties", {})
    segments = properties.get("extras", {}).get("green", {}).get("summary", [])
    distance = properties.get("summary", {}).get("distance", 0)
    valid = [row for row in segments if isinstance(row.get("value"), (int, float))
             and 0 <= row["value"] <= 10 and isinstance(row.get("distance"), (int, float))
             and math.isfinite(row["distance"]) and row["distance"] > 0]
    covered = sum(row["distance"] for row in valid)
    if not distance or covered < distance * 0.95 or covered > distance * 1.05:
        return None
    return sum(row["value"] * row["distance"] for row in valid) / covered


def coordinate_origin(longitude, latitude, *, check=lambda: None):
    address = reverse_origin_address(latitude, longitude, check=check)
    country = str(address.get("country_code", "")).lower()
    if not country:
        raise ValueError("Could not verify the country of this location. Enter a US address or try again.")
    label = ", ".join(dict.fromkeys(str(address[k]) for k in ("city", "town", "county", "state", "country") if address.get(k)))
    return ResidentOrigin(longitude=longitude, latitude=latitude, label=label or f"{latitude:.5f}, {longitude:.5f}", country_code=country)


def named_origins(query, *, check=lambda: None):
    return [ResidentOrigin(longitude=float(row["lon"]), latitude=float(row["lat"]),
                           label=row.get("display_name", query), country_code=row.get("address", {}).get("country_code", ""))
            for row in search_origin_candidates(query, countrycodes="us", check=check)
            if "lon" in row and "lat" in row and row.get("address", {}).get("country_code") == "us"]


class ResidentService:
    def __init__(self, cfg):
        self.cfg = cfg.get("accessibility", {})
        self.base_url = self.cfg.get("ors_base_url") or "https://api.heigit.org/openrouteservice/v2/matrix"
        self.gate = RequestGate(self.cfg.get("ors_request_interval_s", 1.6))
        self.lock = Lock()
        self.searches = {}
        self.tokens = {}
        self.running = {}

    def cancel(self, request_id):
        with self.lock:
            self._prune()
            _, token = self.tokens.setdefault(request_id, (monotonic(), CancellationToken()))
            token.cancel()
            done = self.running.get(request_id)
        # Acknowledge only once the worker has released its socket and lock.
        return done is None or done.wait(3)

    @contextmanager
    def operation(self, request_id, check):
        request_id = request_id or uuid4().hex
        with self.lock:
            self._prune()
            _, token = self.tokens.setdefault(request_id, (monotonic(), CancellationToken()))
            if request_id in self.running:
                raise ValueError("This request is already running.")
            done = self.running[request_id] = Event()
        def checked():
            token.check()
            check()
        try:
            checked()
            yield checked
        finally:
            with self.lock:
                self.running.pop(request_id, None)
                done.set()

    def locate(self, request, *, check=lambda: None):
        with self.operation(request.request_id, check) as checked:
            return coordinate_origin(request.longitude, request.latitude, check=checked)

    def origins(self, request, *, check=lambda: None):
        with self.operation(request.request_id, check) as checked:
            return named_origins(request.query, check=checked)

    def _prune(self):
        now = monotonic()
        self.searches = {key: item for key, item in self.searches.items() if now - item[0] < 1800}
        self.tokens = {key: item for key, item in self.tokens.items() if now - item[0] < 180}

    def _post(self, url, payload, check):
        check()
        self.gate.wait(check)
        api_key = os.getenv("ORS_API_KEY", "")
        if not api_key:
            raise RuntimeError("ORS_API_KEY is required for network reachability. Configure it in the project environment.")
        response = cancellable_request("POST", url, json=payload, headers={"Authorization": api_key}, timeout=(5, 20), check=check)
        if not response.is_success:
            raise RuntimeError(f"The routing provider returned HTTP {response.status_code}. Try again later.")
        check()
        return response.json()

    def _cached_post(self, url, payload, check):
        check()
        identity = json.dumps({"url": url, "payload": payload}, sort_keys=True)
        path = CACHE_DIR / "shared" / "resident" / "v1" / (hashlib.sha256(identity.encode()).hexdigest() + ".json")
        if path.exists() and time() - path.stat().st_mtime < 86400:
            return json.loads(path.read_text(encoding="utf-8"))
        result = self._post(url, payload, check)
        atomic_write_text(path, json.dumps(result))
        return result

    def search(self, request: ResidentSearchRequest, *, check=lambda: None):
        with self.operation(request.request_id, check) as checked:
            return self._search(request, checked)

    def _search(self, request, cancelled):
        started = monotonic()
        def check():
            cancelled()
            if monotonic() - started > 115:
                raise TimeoutError("Nearby search reached its time budget. Narrow the radius and try again.")
        check()
        if request.preferences.needs_clarification:
            raise ValueError(request.preferences.clarification)
        origin = coordinate_origin(request.longitude, request.latitude, check=check)
        if origin.country_code != "us":
            raise ValueError(f"{origin.label} is outside the supported US area. Enter a US address.")
        check()
        places, complete = fetch_nearby_places(request.longitude, request.latitude, request.radius_km, request.preferences, check=check)
        check()
        discovered = len(places)
        # Bound provider work, but explicitly report truncation rather than claim all places.
        limit = 50
        complete = complete and discovered <= limit
        pool = places[:limit]
        checked, reachable = 0, []
        warning = ""
        profile = ORS_PROFILES[request.preferences.mode]
        for offset in range(0, len(pool), 50):
            block = pool[offset:offset + 50]
            payload = {"locations": [[origin.longitude, origin.latitude], *[[p["longitude"], p["latitude"]] for p in block]],
                       "sources": ["0"], "destinations": [str(i + 1) for i in range(len(block))], "metrics": ["duration"]}
            try:
                check()
                result = self._cached_post(f"{self.base_url.rstrip('/')}/{profile}", payload, check)
                durations = result["durations"][0]
                if len(durations) != len(block):
                    raise ValueError("The routing response did not align with the candidate places.")
            except RunCancelled:
                raise
            except (httpx.HTTPError, TimeoutError, RuntimeError, ValueError, KeyError, IndexError) as exc:
                if not checked and isinstance(exc, RuntimeError):
                    raise
                complete, warning = False, f"Some travel times could not be checked ({type(exc).__name__})."
                break
            # A later provider failure preserves checked places, explicitly as a partial result.
            checked += len(block)
            for place, seconds in zip(block, durations):
                if seconds is not None and math.isfinite(float(seconds)) and 0 <= float(seconds) <= request.preferences.threshold_minutes * 60:
                    reachable.append({**place, "duration_minutes": float(seconds) / 60})
        cancelled()
        reachable.sort(key=lambda p: (p["duration_minutes"], p["id"]))
        message = (f"{len(reachable)} places within {request.preferences.threshold_minutes:g} min "
                   f"/ {request.radius_km:g} km.")
        if not complete:
            message += f" Partial results: checked {checked} of {discovered} places. {warning}".rstrip()
        response = ResidentSearchResponse(search_id=uuid4().hex, origin=origin, mode=request.preferences.mode,
            threshold_minutes=request.preferences.threshold_minutes, radius_km=request.radius_km,
            candidates=reachable, complete=complete, candidate_limit=limit,
            discovered_count=discovered, checked_count=checked, message=message)
        with self.lock:
            self._prune()
            if len(self.searches) >= 32:
                self.searches.pop(next(iter(self.searches)))
            self.searches[response.search_id] = (monotonic(), response)
        return response

    def route(self, search_id, candidate_id, request_id=None, *, check=lambda: None):
        with self.operation(request_id, check) as checked:
            return self._route(search_id, candidate_id, checked)

    def _route(self, search_id, candidate_id, check):
        with self.lock:
            self._prune()
            entry = self.searches.get(search_id)
        if entry is None:
            raise KeyError("This nearby search expired. Search again to refresh the places.")
        snapshot = entry[1]
        place = next((p for p in snapshot.candidates if p.id == candidate_id), None)
        if place is None:
            raise ValueError("Choose a reachable candidate from this search.")
        profile = ORS_PROFILES[snapshot.mode]
        payload = {"coordinates": [[snapshot.origin.longitude, snapshot.origin.latitude], [place.longitude, place.latitude]]}
        if snapshot.mode == "walking":
            payload["extra_info"] = ["green"]
        result = self._cached_post(_directions_url(self.base_url, profile), payload, check)
        if not result.get("features"):
            raise ValueError("No detailed route is available for this place.")
        feature = deepcopy(result["features"][0])
        details = feature.get("properties", {}).get("summary", {})
        if feature.get("geometry", {}).get("type") != "LineString":
            raise ValueError("The detailed route did not include a path.")
        if not isinstance(details.get("duration"), (int, float)) or not math.isfinite(details["duration"]) or details["duration"] < 0:
            raise ValueError("The detailed route did not include travel time.")
        green = route_green_score(feature) if snapshot.mode == "walking" else None
        summary = {"destination_id": place.id, "destination_name": place.name, "mode": snapshot.mode,
                   "duration_minutes": details["duration"] / 60, "distance_km": details.get("distance", 0) / 1000,
                   "within_threshold": details["duration"] <= snapshot.threshold_minutes * 60,
                   "coordinate_source": place.coordinate_source, "route_mean_lst_c": None,
                   "green_score": green, "green_status": "available" if green is not None else "unavailable",
                   "heat_status": "unavailable", "selection_method": "ORS route to the selected nearby candidate",
                   "message": ("ORS green index (0-10), weighted by route distance; it is not a temperature or shade measurement." if green is not None else
                               "Route-level greenery data is unavailable for this route. Heat and shade have not been evaluated.")}
        feature["properties"] = summary
        return {"type": "FeatureCollection", "features": [feature], "summary": summary}
