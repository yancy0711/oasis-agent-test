"""Deterministic US place search, canonical cache identity, and FIPS lookup."""
from __future__ import annotations

import hashlib
import json
import re

import geopandas as gpd
import requests
from shapely.geometry import shape

from robustness.cache import CACHE_DIR, CACHE_VERSION, cache_dir, place_slug, scoped_cache
from robustness.retry import RETRYABLE_STATUS, TransientError, with_retry

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
LEVEL_TYPES = {4: {"state"}, 6: {"county"}, 8: {"city", "town", "municipality", "village"}}


class AmbiguousPlaceError(ValueError):
    """Report unresolved geocoder candidates without silently selecting one."""

    def __init__(self, place_name: str, candidates: list[dict]):
        """Store the ambiguous query and lightweight candidate metadata."""
        self.place_name, self.candidates = place_name, candidates
        names = "; ".join(row["display_name"] for row in candidates[:5])
        super().__init__(f"Ambiguous place '{place_name}'. Candidates: {names}")


def _compact(text: str) -> str:
    """Return a lowercase alphanumeric key for canonical matching."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def normalize_place_query(place_name: str) -> str:
    """Remove an optional US suffix so equivalent queries share one cache key."""
    text = re.sub(r",?\s*(usa|u\.s\.a\.|united states(?: of america)?)\s*$", "", place_name.strip(), flags=re.I)
    return re.sub(r"\s*,\s*", ", ", text).strip(" ,")

def _is_short_alias(place_name: str) -> bool:
    """Return whether a bare query is short enough to be an abbreviation."""
    query = normalize_place_query(place_name)
    return "," not in query and bool(re.fullmatch(r"[A-Za-z]{2,3}", query))


def canonical_place_name(candidate: dict) -> str:
    """Build a stable locality, state, country name from Nominatim metadata."""
    address, kind = candidate.get("address", {}), candidate.get("addresstype")
    if kind in LEVEL_TYPES[8]: locality = next((address.get(key) for key in ("city", "town", "municipality", "village") if address.get(key)), None)
    elif kind == "county": locality = address.get("county")
    elif kind == "state": locality = address.get("state")
    else: locality = None
    locality = locality or candidate.get("name") or candidate.get("display_name", "").split(",")[0]
    state = address.get("state") if kind != "state" else None
    country = "USA" if address.get("country_code") == "us" else address.get("country")
    parts = [part for part in (locality, state, country) if part]
    return ", ".join(parts) or candidate.get("display_name", "unknown")


def _candidate_params(place_name: str, limit: int) -> dict:
    """Return stable US-only Nominatim parameters for one normalized query."""
    return {"q": normalize_place_query(place_name), "countrycodes": "us", "format": "jsonv2",
            "polygon_geojson": 1, "addressdetails": 1, "limit": limit}


def _candidate_filename(place_name: str, limit: int = 5, legacy: bool = False) -> str:
    """Return the deterministic candidate-cache filename for one query."""
    params = ({"q": place_name, "format": "jsonv2", "polygon_geojson": 1,
               "addressdetails": 1, "limit": limit} if legacy else _candidate_params(place_name, limit))
    if not legacy: params["q"] = params["q"].casefold()
    key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
    return f"candidates_{key}.json"


def _matches_cached_query(row: dict, place_name: str) -> bool:
    """Return whether canonical boundary parts match a normalized place query."""
    query = [_compact(part) for part in normalize_place_query(place_name).split(",") if part.strip()]
    canonical = [_compact(part) for part in normalize_place_query(canonical_place_name(row)).split(",") if part.strip()]
    return bool(query) and canonical[:len(query)] == query


def _find_cached_candidates(place_name: str, limit: int) -> list[dict] | None:
    """Find an exact query cache or one unique matching canonical cache."""
    root = CACHE_DIR / "places"
    exact = []
    for filename in {_candidate_filename(place_name, limit), _candidate_filename(place_name, limit, legacy=True)}:
        exact.extend(root.glob(f"*/nominatim/{CACHE_VERSION}/{filename}"))
    if exact:
        rows = _merge_candidates(*(json.loads(path.read_text(encoding="utf-8")) for path in exact))
        us_rows = [row for row in rows if row.get("address", {}).get("country_code") == "us"]
        if _is_short_alias(place_name): return us_rows or rows
        matched = [row for row in (us_rows or rows) if _matches_cached_query(row, place_name)]
        return matched or us_rows or rows
    matches = []
    for path in root.glob(f"*/nominatim/{CACHE_VERSION}/candidates_*.json"):
        rows = json.loads(path.read_text(encoding="utf-8"))
        if any(_matches_cached_query(row, place_name) for row in rows): matches.append((path.parents[2].name, rows))
    scopes = {scope for scope, _ in matches}
    return matches[0][1] if len(scopes) == 1 else None


@with_retry(max_retries=3)
def search_place_candidates(place_name: str, limit: int = 5) -> list[dict]:
    """Return cached canonical candidates or perform one live US-only query."""
    if not normalize_place_query(place_name): raise ValueError("place_name must not be empty")
    cached = _find_cached_candidates(place_name, limit)
    if cached is not None: return cached
    response = requests.get(NOMINATIM_URL, params=_candidate_params(place_name, limit),
                            headers={"User-Agent": "oasis-agent/0.1"}, timeout=30)
    if response.status_code in RETRYABLE_STATUS: raise TransientError(f"Nominatim {response.status_code}")
    response.raise_for_status(); return response.json()


def cache_place_candidates(place_name: str, rows: list[dict], selected: dict, limit: int = 5) -> str:
    """Store equivalent query forms under the selected canonical place scope."""
    canonical = canonical_place_name(selected)
    with scoped_cache(canonical) as scope:
        for query, payload in ((canonical, [selected]), (place_name, rows)):
            path = cache_dir("nominatim") / _candidate_filename(query, limit)
            path.write_text(json.dumps(payload), encoding="utf-8")
    return scope


def _merge_candidates(*groups: list[dict]) -> list[dict]:
    """Merge candidate groups while preserving order and OSM identity."""
    merged, seen = [], set()
    for row in (item for group in groups for item in group):
        identity = (row.get("osm_type"), row.get("osm_id"), row.get("display_name"))
        if identity not in seen: seen.add(identity); merged.append(row)
    return merged


def _candidate_priority(row: dict) -> int:
    """Rank supported city and county boundaries before unsupported levels."""
    kind = row.get("addresstype")
    return 0 if kind in LEVEL_TYPES[8] else 1 if kind == "county" else 2


def select_place_candidate(place_name: str, expect_level: int | None = None) -> tuple[dict, list[dict]]:
    """Select one unique US boundary or expose every plausible candidate."""
    all_rows = search_place_candidates(place_name)
    if _is_short_alias(place_name):
        expansions = []
        for city in (row for row in all_rows if row.get("address", {}).get("country_code") == "us"
                     and row.get("addresstype") in LEVEL_TYPES[8]):
            address = city.get("address", {})
            if address.get("county"):
                expansions.append(search_place_candidates(", ".join(filter(None, (address["county"], address.get("state"))))))
        all_rows = _merge_candidates(all_rows, *expansions)
    rows = [row for row in all_rows if row.get("geojson", {}).get("type") in {"Polygon", "MultiPolygon"}]
    rows = [row for row in rows if row.get("address", {}).get("country_code") == "us"] or rows
    expected = LEVEL_TYPES.get(expect_level) if expect_level is not None else None
    if expected:
        rows = [row for row in rows if row.get("addresstype") in expected]
        if not rows: raise ValueError(f"No admin_level={expect_level} boundary matched '{place_name}'")
    elif "county" in place_name.lower(): rows = [row for row in rows if row.get("addresstype") == "county"] or rows
    if not rows: raise ValueError(f"No polygon boundary found for '{place_name}'")
    allowed = set().union(*LEVEL_TYPES.values()) | {"country"}
    viable = sorted([row for row in rows if row.get("addresstype") in allowed] or rows, key=_candidate_priority)
    if len(viable) > 1: raise AmbiguousPlaceError(place_name, viable)
    cache_place_candidates(place_name, all_rows, viable[0])
    return viable[0], viable


def resolve_place(place_name: str, expect_level: int | None = None) -> gpd.GeoDataFrame:
    """Resolve one validated Nominatim candidate into an EPSG:4326 boundary."""
    candidate, candidates = select_place_candidate(place_name, expect_level)
    canonical = canonical_place_name(candidate)
    record = {key: value for key, value in candidate.items() if key != "geojson"}
    boundary = gpd.GeoDataFrame([record], geometry=[shape(candidate["geojson"])], crs="EPSG:4326")
    metric = boundary.estimate_utm_crs(); area_km2 = float(boundary.to_crs(metric).area.sum() / 1_000_000)
    boundary.attrs.update(display_name=candidate.get("display_name", place_name), canonical_name=canonical,
                          cache_scope=place_slug(canonical), place_type=candidate.get("addresstype", "unknown"),
                          admin_level=next((level for level, types in LEVEL_TYPES.items() if candidate.get("addresstype") in types), None),
                          area_km2=area_km2, candidate_count=len(candidates))
    return boundary


@with_retry(max_retries=3)
def _coordinate_fips(lon: float, lat: float) -> dict[str, str]:
    """Reverse-geocode one coordinate into state and county FIPS codes."""
    params = {"x": lon, "y": lat, "benchmark": "Public_AR_Current", "vintage": "Current_Current", "format": "json"}
    key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
    path = cache_dir("census_geocoder") / f"fips_{key}.json"
    if path.exists(): data = json.loads(path.read_text(encoding="utf-8"))
    else:
        response = requests.get("https://geocoding.geo.census.gov/geocoder/geographies/coordinates", params=params, timeout=30)
        if response.status_code in RETRYABLE_STATUS: raise TransientError(f"Census Geocoder {response.status_code}")
        response.raise_for_status(); data = response.json(); path.write_text(json.dumps(data), encoding="utf-8")
    counties = data.get("result", {}).get("geographies", {}).get("Counties", [])
    if not counties: raise ValueError("Census Geocoder returned no US county for this place")
    state_fips, county_fips = str(counties[0].get("STATE", "")).zfill(2), str(counties[0].get("COUNTY", "")).zfill(3)
    if len(state_fips) != 2 or len(county_fips) != 3: raise ValueError("Census Geocoder returned malformed FIPS codes")
    return {"state_fips": state_fips, "county_fips": county_fips}


def resolve_fips(place_name: str, boundary: gpd.GeoDataFrame | None = None,
                 expect_level: int | None = None) -> dict[str, str]:
    """Resolve a US boundary to two-digit state and three-digit county FIPS."""
    boundary = boundary if boundary is not None else resolve_place(place_name, expect_level)
    if boundary.empty: raise ValueError(f"Place not found: {place_name}")
    cache_name = boundary.attrs.get("canonical_name", place_name)
    with scoped_cache(cache_name):
        point = boundary.geometry.union_all().representative_point()
        return _coordinate_fips(float(point.x), float(point.y))
