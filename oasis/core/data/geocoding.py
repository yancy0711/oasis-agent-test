"""Deterministic US place search, canonical cache identity, and FIPS lookup."""
from __future__ import annotations

import hashlib
import json
import re

import geopandas as gpd
import requests
import httpx
from oasis.core.http import cancellable_request
from shapely.geometry import shape

from oasis.core.cache import (
    CACHE_DIR, CACHE_VERSION, atomic_write_text, cache_dir, place_slug, scoped_cache,
)
from oasis.core.retry import RETRYABLE_STATUS, TransientError, with_retry

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
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


def search_origin_candidates(query: str, limit: int = 5, *, countrycodes: str | None = None, viewbox: tuple | None = None, check=lambda: None) -> list[dict]:
    """Try the supplied name, then US centre/center spelling in the same scope."""
    if countrycodes == "us":
        # Expand whole-place shorthand only; never rewrite street/landmark names.
        alias = re.sub(r"[.\s]+", "", query).casefold()
        query = {"la": "Los Angeles, California", "losangeles": "Los Angeles, California",
                 "dc": "Washington, District of Columbia", "nyc": "New York City, New York"}.get(alias, query)
    us_query = re.sub(r"\bcentres\b", "centers", query, flags=re.I)
    us_query = re.sub(r"\bcentre\b", "center", us_query, flags=re.I)
    for variant in dict.fromkeys((query, us_query)):
        rows = _search_origin_candidates(variant, limit=limit, countrycodes=countrycodes,
                                         viewbox=viewbox, check=check)
        if rows:
            return rows
    return []


@with_retry(max_retries=3)
def _search_origin_candidates(query: str, limit: int = 5, *, countrycodes: str | None = None, viewbox: tuple | None = None, check=lambda: None) -> list[dict]:
    """Geocode an address or named point for an on-demand route origin."""
    check()
    query = query.strip()
    if not query:
        raise ValueError("route origin must not be empty")
    params = {
        "q": query, "format": "jsonv2",
        "addressdetails": 1, "limit": limit,
    }
    if countrycodes:
        params["countrycodes"] = countrycodes
    if viewbox is not None:
        west, south, east, north = viewbox
        params.update(viewbox=f"{west},{north},{east},{south}", bounded=1)
    key = hashlib.sha256(
        json.dumps({**params, "q": query.casefold()}, sort_keys=True).encode()
    ).hexdigest()[:16]
    root = CACHE_DIR / "shared" / "nominatim_route_origins" / CACHE_VERSION
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"origin_{key}.json"
    if path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached:
            return cached
    response = cancellable_request(
        "GET", NOMINATIM_URL, params=params, check=check,
        headers={"User-Agent": "oasis-agent/0.1"}, timeout=(5, 30),
    )
    if response.status_code in RETRYABLE_STATUS:
        raise TransientError(f"Nominatim {response.status_code}")
    response.raise_for_status()
    rows = response.json()
    if rows:  # Do not persist empty landmark matches indefinitely.
        atomic_write_text(path, json.dumps(rows))
    return rows


def _landmark_query_variants(query: str) -> list[str]:
    """Expand conversational entrance phrases into geocoder-friendly landmarks."""
    modifier = (
        r"(?:main|principal|front|rear|north(?:ern)?|south(?:ern)?|"
        r"east(?:ern)?|west(?:ern)?|north[- ]?east(?:ern)?|"
        r"north[- ]?west(?:ern)?|south[- ]?east(?:ern)?|"
        r"south[- ]?west(?:ern)?|[A-Z]\d*)"
    )
    access_word = r"(?:entrance|entry|exit|gate|door|access\s+point)"
    cleaned = re.sub(
        rf"^\s*(?:at\s+)?(?:the\s+)?(?:{modifier}\s+)?{access_word}"
        rf"(?:\s+{modifier})?\s+(?:of|to)\s+",
        "",
        query,
        flags=re.I,
    ).strip(" ,")
    variants: list[str] = []
    match = re.fullmatch(r"(.+?)\s+in\s+(.+)", cleaned, flags=re.I)
    landmark, context = (match.group(1).strip(), match.group(2).strip()) if match else (
        cleaned, None
    )
    if context:
        variants.extend((f"{landmark}, {context}", landmark))
    else:
        variants.append(landmark)
    joined = re.sub(
        r"\b([A-Za-z]+)\s+([A-Za-z]+)\s+((?:national\s+)?park)\b",
        lambda item: f"{item.group(1)}{item.group(2)} {item.group(3)}",
        landmark,
        flags=re.I,
    )
    if joined.casefold() != landmark.casefold():
        variants.append(f"{joined}, {context}" if context else joined)
    variants.append(query.strip())
    return list(dict.fromkeys(variant for variant in variants if variant))


def resolve_origin_candidates(query: str, limit: int = 8) -> list[dict]:
    """Resolve an origin from exact address to progressively broader context."""
    park_intent = bool(re.search(r"\bpark\b", query, re.I))
    fallback_rows: list[dict] = []
    for variant in _landmark_query_variants(query):
        rows = search_origin_candidates(variant, limit=limit)
        if not rows:
            continue
        if park_intent:
            preferred = [
                row for row in rows
                if str(row.get("type") or row.get("addresstype") or "").casefold()
                in {"park", "nature_reserve", "protected_area"}
            ]
            if preferred:
                return preferred
            fallback_rows = fallback_rows or rows
            continue
        return rows
    if fallback_rows and len([part for part in query.split(",") if part.strip()]) < 2:
        return fallback_rows
    parts = [part.strip() for part in query.split(",") if part.strip()]
    if len(parts) < 2:
        return []
    subject, context_query = parts[0], ", ".join(parts[1:])
    context_rows = search_origin_candidates(context_query, limit=limit)
    context = next(
        (row for row in context_rows
         if row.get("address", {}).get("country_code") == "us"),
        None,
    )
    if context is None:
        return []
    address = context.get("address", {})
    state = address.get("state")
    if not state:
        return context_rows
    locality = next(
        (address.get(key) for key in ("city", "town", "village") if address.get(key)),
        None,
    )
    county = address.get("county")
    scopes: list[str] = []
    for scope in (locality, county, state):
        if scope and scope.casefold() not in {item.casefold() for item in scopes}:
            scopes.append(scope)
    variants = [
        f"{subject}, {scope}, {state}" if scope != state else f"{subject}, {state}"
        for scope in scopes
    ]
    without_number = re.sub(r"^\s*\d+[A-Za-z-]*\s+", "", subject).strip()
    if without_number and without_number.casefold() != subject.casefold():
        variants.extend(
            f"{without_number}, {scope}, {state}"
            if scope != state else f"{without_number}, {state}"
            for scope in scopes
        )
    broadened: list[list[dict]] = []
    for variant in variants:
        rows = search_origin_candidates(variant, limit=limit)
        if rows:
            broadened.append(rows)
            # Preserve the smallest successful unit; a street-level fallback is
            # used only when the numbered address also has no match.
            break
    return _merge_candidates(*broadened) if broadened else context_rows


@with_retry(max_retries=3)
def reverse_origin_address(latitude: float, longitude: float, *, check=lambda: None) -> dict:
    """Return a cached county-level address for a resolved origin coordinate."""
    check()
    params = {
        "lat": round(float(latitude), 6), "lon": round(float(longitude), 6),
        "format": "jsonv2", "addressdetails": 1, "zoom": 10,
    }
    key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
    root = CACHE_DIR / "shared" / "nominatim_route_origins" / CACHE_VERSION
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"reverse_{key}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8")).get("address", {})
    response = cancellable_request(
        "GET", NOMINATIM_REVERSE_URL, params=params, check=check,
        headers={"User-Agent": "oasis-agent/0.1"}, timeout=(5, 30),
    )
    if response.status_code in RETRYABLE_STATUS:
        raise TransientError(f"Nominatim {response.status_code}")
    response.raise_for_status()
    payload = response.json()
    atomic_write_text(path, json.dumps(payload))
    return payload.get("address", {})


def _route_parent(row: dict) -> tuple[str | None, str | None]:
    """Resolve the smallest supported parent, including point reverse lookup."""
    address = row.get("address", {})
    locality = next(
        (address.get(key) for key in ("city", "town", "municipality", "village") if address.get(key)),
        None,
    )
    parent, state = locality or address.get("county"), address.get("state")
    if parent and state:
        return parent, state
    latitude, longitude = row.get("lat"), row.get("lon")
    if latitude is None or longitude is None:
        return parent, state
    try:
        reverse = reverse_origin_address(float(latitude), float(longitude))
    except (requests.RequestException, httpx.HTTPError, TransientError, ValueError, json.JSONDecodeError):
        return parent, state
    reverse_locality = next(
        (reverse.get(key) for key in ("city", "town", "municipality", "village") if reverse.get(key)),
        None,
    )
    return parent or reverse_locality or reverse.get("county"), state or reverse.get("state")


def infer_route_study_area(query: str) -> tuple[str, str]:
    """Resolve a named route origin to its smallest supported US city or county."""
    rows = resolve_origin_candidates(query)
    row = next(
        (item for item in rows if item.get("address", {}).get("country_code") == "us"),
        None,
    )
    if row is None:
        raise ValueError(
            "The route origin could not be matched to a US city or county. "
            "Enter the study city or county manually."
        )
    parent, state = _route_parent(row)
    if not parent or not state:
        raise ValueError(
            "The route origin was found, but its city or county could not be identified. "
            "Enter the study city or county manually."
        )
    label = row.get("display_name") or query
    return label, f"{parent}, {state}, USA"


def route_origin_options(
    entity: str,
    context: str = "",
    positional_hint: str = "",
    *,
    fallback_language: bool = False,
    limit: int = 8,
) -> tuple[list[dict], bool]:
    """Search a bounded origin interpretation and return ranked US choices."""
    queries = [f"{entity}, {context}" if context else entity, entity]
    groups = []
    exact_administrative: list[dict] = []
    if not context and not positional_hint and len(entity.split()) >= 2:
        try:
            boundary_rows = search_place_candidates(entity, limit=limit)
        except (requests.RequestException, httpx.HTTPError, TransientError, ValueError, json.JSONDecodeError):
            # This optional exact-boundary probe must not block origin search.
            boundary_rows = []
        exact_administrative = [
            row for row in boundary_rows
            if row.get("address", {}).get("country_code") == "us"
            and row.get("addresstype") in (LEVEL_TYPES[8] | {"county"})
            and _compact(canonical_place_name(row).split(",", 1)[0]) == _compact(entity)
        ]
        if exact_administrative:
            groups.append(exact_administrative)
    for query in dict.fromkeys(queries):
        rows = (
            resolve_origin_candidates(query, limit=limit)
            if fallback_language else search_origin_candidates(query, limit=limit)
        )
        if rows:
            groups.append(rows)
    if not groups and context:
        context_rows = search_origin_candidates(context, limit=limit)
        if context_rows:
            groups.append(context_rows)
    rows = _merge_candidates(*groups)
    if exact_administrative:
        rows = exact_administrative
    admin_types = set().union(*LEVEL_TYPES.values())
    preferred_types = {
        "house": 0, "building": 0, "amenity": 0, "park": 0,
        "nature_reserve": 0, "protected_area": 0, "tourism": 0,
        "road": 1, "residential": 1,
    }
    rows.sort(key=lambda row: preferred_types.get(
        str(row.get("type") or row.get("addresstype") or "").casefold(), 2
    ))
    choices = []
    seen = set()
    for index, row in enumerate(rows):
        address = row.get("address", {})
        if address.get("country_code") != "us":
            continue
        kind = str(row.get("addresstype") or row.get("type") or "location")
        # A fully named multiword city in a route request can serve as an
        # approximate, user-confirmed origin at Nominatim's representative
        # coordinate. Short aliases such as "DC" remain study-area queries.
        exact_route_origin = bool(exact_administrative) and row in exact_administrative
        administrative = kind in admin_types and not exact_route_origin
        if administrative:
            parent_place = canonical_place_name(row)
        else:
            parent, state = _route_parent(row)
            if not parent or not state:
                continue
            parent_place = f"{parent}, {state}, USA"
        display = str(row.get("display_name") or query)
        longitude, latitude = row.get("lon"), row.get("lat")
        identity = (display.casefold(), parent_place.casefold())
        if identity in seen:
            continue
        seen.add(identity)
        choices.append({
            "candidate_id": str(index), "display_name": display,
            "place_name": parent_place, "place_type": kind,
            "entity": entity, "context": context,
            "positional_hint": positional_hint,
            "administrative": administrative,
            "longitude": float(longitude) if longitude is not None else None,
            "latitude": float(latitude) if latitude is not None else None,
        })
    short_admin_alias = bool(re.fullmatch(r"[A-Za-z]{2,3}", entity.strip()))
    administrative_query = bool(choices) and (
        short_admin_alias
        or all(choice["administrative"] for choice in choices)
    )
    return choices, administrative_query


def cache_place_candidates(place_name: str, rows: list[dict], selected: dict, limit: int = 5) -> str:
    """Store equivalent query forms under the selected canonical place scope."""
    canonical = canonical_place_name(selected)
    with scoped_cache(canonical) as scope:
        for query, payload in ((canonical, [selected]), (place_name, rows)):
            path = cache_dir("nominatim") / _candidate_filename(query, limit)
            atomic_write_text(path, json.dumps(payload))
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
    us_rows = [row for row in rows if row.get("address", {}).get("country_code") == "us"]
    if rows and not us_rows:
        raise ValueError(
            f"'{place_name}' resolved outside the United States. "
            "OASIS currently supports US city and county study areas only."
        )
    rows = us_rows
    if not rows:
        try:
            unrestricted = search_origin_candidates(place_name, limit=5)
        except (requests.RequestException, httpx.HTTPError, TransientError, ValueError, json.JSONDecodeError):
            unrestricted = []
        if unrestricted and not any(
            row.get("address", {}).get("country_code") == "us" for row in unrestricted
        ):
            raise ValueError(
                f"'{place_name}' resolved outside the United States. "
                "OASIS currently supports US city and county study areas only."
            )
    expected = LEVEL_TYPES.get(expect_level) if expect_level is not None else None
    if expected:
        rows = [row for row in rows if row.get("addresstype") in expected]
        if not rows: raise ValueError(f"No admin_level={expect_level} boundary matched '{place_name}'")
    elif "county" in place_name.lower(): rows = [row for row in rows if row.get("addresstype") == "county"] or rows
    if not rows: raise ValueError(f"No polygon boundary found for '{place_name}'")
    supported = LEVEL_TYPES[8] | {"county"}
    viable = [row for row in rows if row.get("addresstype") in supported]
    if not viable and expect_level is None:
        fallback_queries = []
        for row in rows:
            address, state = row.get("address", {}), row.get("address", {}).get("state")
            if not state:
                continue
            for key in ("city", "municipality", "town", "village", "county"):
                parent = address.get(key)
                query = f"{parent}, {state}" if parent else ""
                if query and query.casefold() not in {item.casefold() for item in fallback_queries}:
                    fallback_queries.append(query)
        expanded = [search_place_candidates(query) for query in fallback_queries]
        all_rows = _merge_candidates(all_rows, *expanded)
        rows = [row for row in all_rows if row.get("geojson", {}).get("type") in {"Polygon", "MultiPolygon"}]
        viable = [row for row in rows if row.get("addresstype") in supported]
    if not viable:
        raise ValueError(
            f"No supported city or county boundary matched '{place_name}'. "
            "Choose a city or county from the available location options."
        )
    viable = sorted(viable, key=_candidate_priority)
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
        response.raise_for_status(); data = response.json(); atomic_write_text(path, json.dumps(data))
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
