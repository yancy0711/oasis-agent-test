"""Bounded OSM discovery for residents; planner categories remain unchanged."""
import hashlib
import json
import math
import os
import time

from oasis.core.cache import CACHE_DIR, atomic_write_text
from oasis.core.data.overpass import _fetch_payload, DEFAULT_ENDPOINTS, OverpassUnavailable, payload_error

# OR between selectors; predicates within a selector are ANDed.
CATEGORY_SELECTORS = {
    "library": ['["amenity"="library"]'],
    "community_centre": ['["amenity"="community_centre"]'],
    "swimming_pool": ['["leisure"="swimming_pool"]',
                      '["leisure"="sports_centre"]["sport"~"(^|;)swimming(;|$)"]'],
    "mall": ['["shop"="mall"]'],
    "sports_centre": ['["leisure"="sports_centre"]'],
    "arts_centre": ['["amenity"="arts_centre"]'],
    "place_of_worship": ['["amenity"="place_of_worship"]'],
    "park": ['["leisure"="park"]'],
}


def category_for(tags):
    if tags.get("shop") == "mall":
        return "mall"
    if tags.get("leisure") == "swimming_pool" or (
        tags.get("leisure") == "sports_centre" and "swimming" in tags.get("sport", "").split(";")
    ):
        return "swimming_pool"
    if tags.get("amenity") in {"library", "community_centre", "arts_centre", "place_of_worship"}:
        return tags["amenity"]
    if tags.get("leisure") in {"sports_centre", "park"}:
        return tags["leisure"]
    return None


def distance_km(origin, destination):
    lon1, lat1, lon2, lat2 = map(math.radians, [*origin, *destination])
    a = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 6371 * 2 * math.asin(min(1, math.sqrt(a)))


def normalize_places(elements, preferences, origin, radius_km):
    """Retain access evidence and stable OSM IDs, never infer cooling status."""
    allowed = set(preferences.categories) - set(preferences.excluded_categories)
    nodes = {row["id"]: row for row in elements if row.get("type") == "node"}
    result = {}
    for row in elements:
        tags = row.get("tags", {})
        category = category_for(tags)
        if category not in allowed or tags.get("access") in {"private", "no", "members", "permit"}:
            continue
        if preferences.indoor_only and tags.get("indoor") != "yes":
            continue
        location = row if "lon" in row else row.get("center", {})
        if "lon" not in location or "lat" not in location:
            continue
        coordinate_source = "Mapped facility point" if row["type"] == "node" else "Mapped facility center; entrance unverified"
        entrances = [nodes[node] for node in row.get("nodes", []) if node in nodes
                     and nodes[node].get("tags", {}).get("entrance") in {"yes", "main"}
                     and nodes[node].get("tags", {}).get("access") not in {"private", "no"}]
        if entrances:
            location = min(entrances, key=lambda n: distance_km(origin, [n["lon"], n["lat"]]))
            coordinate_source = "Mapped facility entrance"
        if distance_km(origin, [location["lon"], location["lat"]]) > radius_km:
            continue
        identity = f"{row['type']}/{row['id']}"
        result[identity] = {
            "id": identity, "name": tags.get("name") or category.replace("_", " ").title(),
            "category": category, "longitude": float(location["lon"]), "latitude": float(location["lat"]),
            "opening_hours": tags.get("opening_hours", "Unknown"), "access": tags.get("access", "Unknown"),
            "fee": tags.get("fee", "Unknown"), "indoor": tags.get("indoor", "Unknown"),
            "coordinate_source": coordinate_source, "osm_url": f"https://www.openstreetmap.org/{identity}",
        }
    return sorted(result.values(), key=lambda p: (distance_km(origin, [p["longitude"], p["latitude"]]), p["id"]))


def fetch_nearby_places(longitude, latitude, radius_km, preferences, *, timeout=45, check=lambda: None):
    allowed = sorted(set(preferences.categories) - set(preferences.excluded_categories))
    if not allowed:
        return [], True
    selectors = [selector for category in allowed for selector in CATEGORY_SELECTORS[category]]
    def query_for(lon, lat, radius):
        around = f"(around:{radius * 1000:.0f},{lat:.6f},{lon:.6f})"
        return ("[out:json][timeout:12][maxsize:134217728];(" + "".join(f"nwr{selector}{around};" for selector in selectors)
                + ')->.sites;.sites out center;node(w.sites)["entrance"];out;')
    configured = os.getenv("RESIDENT_OVERPASS_URL") or os.getenv("OASIS_OVERPASS_URL")
    endpoints = [configured] if configured else DEFAULT_ENDPOINTS
    directory = CACHE_DIR / "shared" / "resident" / "v1"
    old_query = query_for(longitude, latitude, radius_km).replace("[maxsize:134217728]", "")
    old_key = hashlib.sha256(f"{endpoints[0]}\n{old_query}".encode()).hexdigest()[:24]
    old_path = directory / f"places_{old_key}.json"
    if old_path.exists() and time.time() - old_path.stat().st_mtime < 86400:
        payload = json.loads(old_path.read_text(encoding="utf-8"))
    else:
        # Maximum center offset in a 0.01-degree cell is <0.8 km. Padding
        # covers the requested circle; filter raw elements at the actual origin.
        query = query_for(round(longitude, 2), round(latitude, 2), radius_km + 1)
        # Resource budgets do not change successful query contents. Keep the
        # historical identity so increasing RAM does not discard usable caches.
        cache_query = query.replace("[maxsize:134217728]", "[maxsize:33554432]")
        key = hashlib.sha256(f"{endpoints[0]}\n{cache_query}".encode()).hexdigest()[:24]
        path = directory / f"region_{key}.json"
        if path.exists() and time.time() - path.stat().st_mtime < 86400:
            payload = json.loads(path.read_text(encoding="utf-8"))
        else:
            payload = _fetch_payload(endpoints, query, key, timeout, check)
            check()
            if not payload_error(payload):
                atomic_write_text(path, json.dumps(payload))
    error = payload_error(payload)
    if error:
        raise OverpassUnavailable(f"OpenStreetMap lookup returned {error}. Retry to get a complete candidate set.")
    return normalize_places(payload["elements"], preferences, [longitude, latitude], radius_km), True
