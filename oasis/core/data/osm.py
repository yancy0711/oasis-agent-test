"""OpenStreetMap: supply-side candidate facilities.

An empty query result is NOT an exception here: it returns a structured
ToolError with a repair hint, which is exactly what the agent's
self-correction loop consumes (e.g. small towns without libraries).
"""
from __future__ import annotations
import geopandas as gpd
import hashlib
import json
import os
import re
import time
import requests
from urllib.parse import urlparse
from collections import OrderedDict
from oasis.contracts.domain import ToolError
from oasis.core.cache import cache_dir, atomic_write_text
from oasis.core.data.overpass import _fetch_payload, DEFAULT_ENDPOINTS, OverpassUnavailable, payload_error
from oasis.core.spatial.alignment import centroids, pick_metric_crs

# Semantic amenity -> OSM tags. Extending this set is how the agent
# self-corrects when a query comes back empty.
AMENITY_TAGS = {
    "library": {"amenity": "library"},
    "community_centre": {"amenity": "community_centre"},
    "school": {"amenity": "school"},
    "place_of_worship": {"amenity": "place_of_worship"},
    "sports_centre": {"leisure": "sports_centre"},
    "arts_centre": {"amenity": "arts_centre"},
}


def _bounded_features_query(coords: str, tags: dict[str, list[str]], execution_timeout_s: int = 20, *, use_bbox: bool = False) -> str:
    """Union exact tag matches without repeating the polygon for each OSM type."""
    spatial_filter = f'poly:"{coords}"'
    if use_bbox:
        values = list(map(float, coords.split()))
        latitudes, longitudes = values[::2], values[1::2]
        # Query a superset of each polygon part, then let _create_gdf apply the
        # original exact study boundary. Padding covers coordinate rounding.
        spatial_filter = f"{min(latitudes)-1e-6},{min(longitudes)-1e-6},{max(latitudes)+1e-6},{max(longitudes)+1e-6}"
    selectors = []
    for key, values in tags.items():
        pattern = "|".join(re.escape(value) for value in sorted(set(values)))
        selectors.append(f'nwr["{key}"~"^({pattern})$"]({spatial_filter});')
    memory_bytes = 134217728 if use_bbox else 67108864
    return f"[out:json][timeout:{execution_timeout_s}][maxsize:{memory_bytes}];(" + "".join(selectors) + ");(._;>;);out;"


def _numeric_tag(values):
    """Parse only unambiguous positive numeric OSM tag values."""
    parsed = values.astype("string").str.strip().str.replace(",", "", regex=False)
    numeric = parsed.where(parsed.str.fullmatch(r"[0-9]+(?:\.[0-9]+)?", na=False))
    result = numeric.astype(float)
    return result.where(result.gt(0))


def enrich_capacity_evidence(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Preserve observed capacity and relative floor-area evidence before centroids.

    Gross floor area is deliberately a relative proxy, not a people capacity:
    usable area and occupancy standards are facility- and jurisdiction-specific.
    """
    result = gdf.copy()
    metric = result.to_crs(pick_metric_crs(result))
    polygon = metric.geometry.geom_type.isin(["Polygon", "MultiPolygon"])
    result["footprint_area_m2"] = metric.geometry.area.where(polygon)
    if "building:levels" in result:
        levels = _numeric_tag(result["building:levels"])
    else:
        levels = result["footprint_area_m2"] * float("nan")
    result["building_levels"] = levels
    result["gross_floor_area_proxy_m2"] = result["footprint_area_m2"] * levels.fillna(1.0)
    if "capacity" in result:
        result["capacity_observed_people"] = _numeric_tag(result["capacity"])
    else:
        result["capacity_observed_people"] = float("nan")
    result["capacity_evidence"] = "unknown"
    result.loc[result["gross_floor_area_proxy_m2"].notna(), "capacity_evidence"] = "relative_floor_area_proxy"
    result.loc[result["capacity_observed_people"].notna(), "capacity_evidence"] = "observed_osm_capacity"
    return result


def fetch_candidates(boundary: gpd.GeoDataFrame,
                     amenities: list[str], *, check=lambda: None, progress=None) -> tuple[gpd.GeoDataFrame | None, ToolError | None]:
    """Fetch point candidates for the requested OSM amenity types."""
    import osmnx as ox
    ox.settings.use_cache = True
    ox.settings.cache_folder = str(cache_dir("osmnx"))
    
    tags: dict[str, list[str]] = {}
    for a in amenities:
        for k, v in AMENITY_TAGS.get(a, {}).items():
            tags.setdefault(k, []).append(v)
    # Reuse OSMnx geometry conversion and polygon subdivision, but not its
    # transport (180s status requests and recursive 55s retries in OSMnx 2.1).
    from osmnx import _overpass, _http, features
    from osmnx._errors import InsufficientResponseError
    polygon = boundary.geometry.union_all()
    if not polygon.is_valid or polygon.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError("Study boundary must be a valid Polygon or MultiPolygon")
    polygons = _overpass._make_overpass_polygon_coord_strs(polygon)
    endpoint = os.getenv("OASIS_OVERPASS_URL")
    endpoints = [endpoint] if endpoint else DEFAULT_ENDPOINTS
    hosted = bool(endpoint and urlparse(endpoint).hostname == "api.overpass.ch")
    deadline = time.monotonic() + 100
    acquisition = {"cache_parts": 0, "live_parts": 0}
    def remote_check():
        check()
        if time.monotonic() >= deadline:
            raise OverpassUnavailable("OpenStreetMap candidate lookup reached its 100-second budget. Completed query parts are cached; retry later.")
    def responses():
        for index, coords in enumerate(polygons):
            check()
            if progress:
                progress(f"Candidate facilities: OSM area {index+1}/{len(polygons)} (cache or bounded request)", 0, 3, unit="stages")
            legacy = _overpass._create_overpass_features_query(coords, tags)
            # Keep existing OSMnx caches usable, including their historical URL.
            legacy_url = str(requests.Request("GET", ox.settings.overpass_url.rstrip("/") + "/interpreter", params=OrderedDict(data=legacy)).prepare().url)
            payload = _http._retrieve_from_cache(legacy_url)
            historical_query = re.sub(r"\[timeout:\d+\]", "[timeout:20]", legacy)
            historical_query = re.sub(r"\[maxsize:\d+\]", "", historical_query).replace("[timeout:20]", "[timeout:20][maxsize:67108864]")
            # Equivalent query; retain successful caches from the older spelling.
            key = hashlib.sha256((str(endpoints) + historical_query).encode()).hexdigest()[:24]
            path = cache_dir("osmnx") / f"bounded_{key}.json"
            if payload is None and path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
            if payload is None:
                if progress:
                    progress(f"Candidate facilities: OSM area {index+1}/{len(polygons)} (live request)", 0, 3, unit="stages")
                remote_check()
                # Hosted accounts allow a longer city query; leave room for
                # execution outside the public-instance budget. The 100s total
                # deadline and cooperative cancellation still bound this call.
                query = _bounded_features_query(coords, tags, 45 if hosted else 20, use_bbox=hosted)
                payload = _fetch_payload(endpoints, query, key, 65 if hosted else 45, remote_check)
                acquisition["live_parts"] += 1
                if not payload_error(payload):
                    atomic_write_text(path, json.dumps(payload))
            else:
                acquisition["cache_parts"] += 1
                if progress:
                    progress(f"Candidate facilities: OSM area {index+1}/{len(polygons)} (cached)", 0, 3, unit="stages")
            error = payload_error(payload)
            if error:
                raise OverpassUnavailable(f"OpenStreetMap candidate lookup returned {error}. No partial candidate set was optimized.")
            check()
            yield payload
    try:
        gdf = features._create_gdf(responses(), polygon, tags)
    except InsufficientResponseError:
        gdf = None
    if gdf is None or gdf.empty:
        return None, ToolError(
            kind="not_found",
            message=f"No facilities of type {amenities} found in this area",
            hint="Broaden amenity types (add place_of_worship / sports_centre) "
                 "or enlarge the study area")
    gdf = enrich_capacity_evidence(gdf.reset_index())
    gdf = centroids(gdf)
    gdf["cand_id"] = gdf.index.astype(str)
    gdf.attrs["osm_acquisition"] = acquisition

    return gdf, None
