"""Shared schema for candidate CSVs and their map preview."""

from pathlib import Path
from io import BytesIO

import geopandas as gpd
import pandas as pd

from oasis.core.spatial.coordinates import normalize_wgs84


def read_candidate_csv(path: Path | BytesIO) -> tuple[pd.DataFrame, str, str]:
    frame = pd.read_csv(path)
    columns = {name.lower(): name for name in frame.columns}
    lat = columns.get("latitude", columns.get("lat"))
    lon = columns.get("longitude", columns.get("lon", columns.get("lng")))
    if not lat or not lon:
        raise ValueError("Candidate CSV must contain latitude/longitude or lat/lon columns")
    return frame, lat, lon


def normalized_candidate_csv(content: bytes, coordinate_system: str, epsg: int) -> bytes:
    """Store transformed candidate values so visualization and solver use one coordinate set."""
    frame, lat, lon = read_candidate_csv(BytesIO(content))
    if frame.empty:
        raise ValueError("The candidate CSV contains no points.")
    x, y = pd.to_numeric(frame[lon], errors="raise"), pd.to_numeric(frame[lat], errors="raise")
    points = gpd.GeoDataFrame(geometry=gpd.points_from_xy(x, y))
    points = normalize_wgs84(points, coordinate_system, epsg)
    frame[lon], frame[lat] = points.geometry.x, points.geometry.y
    frame["source_crs"] = "GCJ-02 (approximate inverse)" if coordinate_system == "gcj02" else f"EPSG:{epsg}"
    return frame.to_csv(index=False).encode("utf-8")


def preview_candidate_csv(path: Path) -> list[tuple[float, float, str]]:
    """Return only valid WGS84 coordinates and labels for display."""
    frame, lat, lon = read_candidate_csv(path)
    x = pd.to_numeric(frame[lon], errors="coerce")
    y = pd.to_numeric(frame[lat], errors="coerce")
    names = next((frame[column] for column in ("name", "label", "cand_id") if column in frame),
                 pd.Series("", index=frame.index))
    valid = x.between(-180, 180) & y.between(-90, 90)
    return [(float(longitude), float(latitude), str(name)[:150])
            for longitude, latitude, name in zip(x[valid], y[valid], names[valid].fillna(""))]
