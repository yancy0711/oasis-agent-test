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
from oasis.core.spatial.alignment import centroids

# Semantic amenity -> OSM tags. Extending this set is how the agent
# self-corrects when a query comes back empty.
AMENITY_TAGS = {
    "library": {"amenity": "library"},
    "community_centre": {"amenity": "community_centre"},
    "school": {"amenity": "school"},
    "place_of_worship": {"amenity": "place_of_worship"},
    "sports_centre": {"leisure": "sports_centre"},
}


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
    gdf = gdf.reset_index()
    gdf = centroids(gdf)
    gdf["cand_id"] = gdf.index.astype(str)

    return gdf, None
