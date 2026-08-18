"""Versioned fine-grid population providers used for exposure weighting."""
from __future__ import annotations

from typing import Any


WORLDPOP_DATA = "https://data.worldpop.org/GIS/Population/Global_2015_2030"


def worldpop_raster_url(year: int, config: dict[str, Any] | None = None) -> str:
    """Return one version-pinned WorldPop 100 m constrained population GeoTIFF."""
    cfg = config or {}
    if cfg.get("url"):
        return str(cfg["url"])
    release = str(cfg.get("release", "R2025A"))
    version = str(cfg.get("version", "v1"))
    country = str(cfg.get("country", "USA")).upper()
    resolution = str(cfg.get("resolution", "100m"))
    kind = "CN" if cfg.get("constrained", True) else "UN"
    filename = f"{country.lower()}_pop_{int(year)}_{kind}_{resolution}_{release}_{version}.tif"
    return (
        f"{WORLDPOP_DATA}/{release}/{int(year)}/{country}/{version}/"
        f"{resolution}/{'constrained' if kind == 'CN' else 'unconstrained'}/{filename}"
    )


def population_raster_source(year: int, config: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Resolve a configured fine-grid population source without reading raster data."""
    cfg = config or {}
    if not cfg.get("enabled", True):
        return None
    provider = str(cfg.get("provider", "census_block_group")).lower()
    if provider not in {"census_block_group", "worldpop", "geotiff"}:
        raise ValueError(f"Unsupported population-grid provider: {provider}")
    if provider == "census_block_group":
        return {
            "provider": provider, "year": int(year), "resolution": "block group",
            "minimum_valid_population_share": float(cfg.get("minimum_valid_population_share", 0.8)),
        }
    url = worldpop_raster_url(year, cfg) if provider == "worldpop" else cfg.get("url")
    if not url:
        raise ValueError("A population GeoTIFF URL or local path is required")
    return {
        "provider": provider,
        "url": str(url),
        "year": int(year),
        "resolution": str(cfg.get("resolution", "100m")),
        "release": str(cfg.get("release", "custom")),
        "minimum_valid_population_share": float(cfg.get("minimum_valid_population_share", 0.8)),
    }
