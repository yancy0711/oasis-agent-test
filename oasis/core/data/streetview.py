"""Retrieve the nearest Mapillary image without exposing the server token."""
from __future__ import annotations

import json
import math
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

MAPILLARY_IMAGES_URL = "https://graph.mapillary.com/images"


def _read_json(request: Request, *, timeout: int = 8, attempts: int = 2) -> dict:
    """Read a small JSON response, retrying one transient network timeout."""
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError:
            raise
        except (URLError, TimeoutError):
            if attempt + 1 >= attempts:
                raise
    raise RuntimeError("The Mapillary request did not complete.")


def _distance_m(latitude: float, longitude: float, other_latitude: float, other_longitude: float) -> float:
    """Return great-circle distance in metres for two WGS84 coordinates."""
    radius = 6_371_000.0
    lat1, lat2 = math.radians(latitude), math.radians(other_latitude)
    dlat = lat2 - lat1
    dlon = math.radians(other_longitude - longitude)
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(value))


def nearest_streetview(latitude: float, longitude: float, radius_m: int = 100) -> dict:
    """Return the nearest Mapillary thumbnail inside a bounded search radius."""
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("Latitude or longitude is outside the valid WGS84 range.")
    if not 25 <= radius_m <= 1000:
        raise ValueError("Street-level image radius must be between 25 and 1000 metres.")
    token = os.getenv("MAPILLARY_ACCESS_TOKEN", "").strip()
    if not token:
        return {
            "available": False, "status": "not_configured", "provider": "Mapillary",
            "message": "Nearby imagery is unavailable because the server has no Mapillary access token.",
        }

    lat_delta = radius_m / 111_320
    lon_delta = radius_m / max(1.0, 111_320 * math.cos(math.radians(latitude)))
    params = urlencode({
        "bbox": f"{longitude - lon_delta},{latitude - lat_delta},{longitude + lon_delta},{latitude + lat_delta}",
        "fields": "id,computed_geometry,is_pano",
        "limit": 100,
    })
    request = Request(
        f"{MAPILLARY_IMAGES_URL}?{params}",
        headers={"Authorization": f"OAuth {token}", "User-Agent": "OASIS-Agent/1.0"},
    )
    try:
        rows = _read_json(request).get("data", [])
    except HTTPError as exc:
        return {
            "available": False, "status": "error", "provider": "Mapillary",
            "message": f"Mapillary image search failed (HTTP {exc.code}).",
        }
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {
            "available": False, "status": "error", "provider": "Mapillary",
            "message": f"Mapillary image search failed: {type(exc).__name__}.",
        }

    matches = []
    for row in rows:
        coordinates = (row.get("computed_geometry") or {}).get("coordinates") or []
        if len(coordinates) < 2:
            continue
        distance = _distance_m(latitude, longitude, float(coordinates[1]), float(coordinates[0]))
        if distance <= radius_m:
            matches.append((distance, row, coordinates))
    if not matches:
        return {
            "available": False, "status": "no_image", "provider": "Mapillary",
            "message": f"No Mapillary image was found within {radius_m} metres.",
        }
    # Choose the closest image; prefer a panorama only when distances tie.
    distance, row, coordinates = min(
        matches, key=lambda item: (round(item[0], 1), not bool(item[1].get("is_pano")))
    )
    image_id = str(row["id"])
    metadata_params = urlencode({"fields": "id,captured_at,thumb_1024_url,creator"})
    metadata_request = Request(
        f"https://graph.mapillary.com/{image_id}?{metadata_params}",
        headers={"Authorization": f"OAuth {token}", "User-Agent": "OASIS-Agent/1.0"},
    )
    try:
        metadata = _read_json(metadata_request)
    except HTTPError as exc:
        return {
            "available": False, "status": "error", "provider": "Mapillary",
            "message": f"Mapillary image metadata failed (HTTP {exc.code}).",
        }
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {
            "available": False, "status": "error", "provider": "Mapillary",
            "message": f"Mapillary image metadata failed: {type(exc).__name__}.",
        }
    image_url = metadata.get("thumb_1024_url")
    if not image_url:
        return {
            "available": False, "status": "no_image", "provider": "Mapillary",
            "message": "The nearest Mapillary record has no displayable thumbnail.",
        }
    creator = metadata.get("creator") if isinstance(metadata.get("creator"), dict) else {}
    return {
        "available": True, "status": "available", "provider": "Mapillary",
        "image_id": image_id, "image_url": image_url,
        "viewer_url": f"https://www.mapillary.com/app/?pKey={image_id}&focus=photo",
        "captured_at": metadata.get("captured_at"), "creator": creator.get("username"),
        "latitude": float(coordinates[1]), "longitude": float(coordinates[0]),
        "distance_m": round(distance, 1),
        "message": f"Nearest Mapillary image is {distance:.0f} metres from the selected facility.",
    }
