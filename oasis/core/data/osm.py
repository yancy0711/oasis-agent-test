"""OpenStreetMap: supply-side candidate facilities.

An empty query result is NOT an exception here: it returns a structured
ToolError with a repair hint, which is exactly what the agent's
self-correction loop consumes (e.g. small towns without libraries).
"""
from __future__ import annotations
import geopandas as gpd
from oasis.contracts.domain import ToolError
from oasis.core.retry import with_retry
from oasis.core.cache import cache_dir
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


@with_retry(max_retries=3)
def fetch_candidates(boundary: gpd.GeoDataFrame,
                     amenities: list[str]) -> tuple[gpd.GeoDataFrame | None, ToolError | None]:
    """Fetch point candidates for the requested OSM amenity types."""
    import osmnx as ox
    ox.settings.use_cache = True
    ox.settings.cache_folder = str(cache_dir("osmnx"))
    
    tags: dict[str, list[str]] = {}
    for a in amenities:
        for k, v in AMENITY_TAGS.get(a, {}).items():
            tags.setdefault(k, []).append(v)
    gdf = ox.features_from_polygon(boundary.geometry.union_all(), tags)
    if gdf is None or gdf.empty:
        return None, ToolError(
            kind="not_found",
            message=f"No facilities of type {amenities} found in this area",
            hint="Broaden amenity types (add place_of_worship / sports_centre) "
                 "or enlarge the study area")
    gdf = enrich_capacity_evidence(gdf.reset_index())
    gdf = centroids(gdf)
    gdf["cand_id"] = gdf.index.astype(str)

    return gdf, None
