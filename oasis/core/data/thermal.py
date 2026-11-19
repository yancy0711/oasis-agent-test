"""Read and aggregate ERA5-HEAT on its native grid."""

from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from oasis.core.data.heat import DEFAULT_LST_RESOLUTION_M

UTCI_SOURCE = "Copernicus Climate Data Store / ECMWF ERA5-HEAT"
UTCI_URL = "https://doi.org/10.24381/cds.553b7518"
UTCI_GRID = 0.25
UTCI_AGGREGATION = "Mean of daily peak hourly UTCI; nearest native grid cell to each tract representative point."
LST_SOURCE = "Landsat 8/9 Collection 2 Level-2 via Microsoft Planetary Computer"


def utci_paths(cfg: dict) -> list[Path]:
    """Accept one regional NetCDF or a directory of original daily NetCDF files."""
    data = cfg.get("data", {})
    value = data.get("utci_path") or data.get("sf_utci_path")
    if not value:
        return []
    path = Path(value)
    if path.is_dir():
        return sorted(path.rglob("*.nc"))
    return [path] if path.is_file() and path.suffix.lower() == ".nc" else []


def utci_cells(tracts: gpd.GeoDataFrame) -> tuple[np.ndarray, np.ndarray]:
    points = tracts.to_crs(4326).representative_point()
    if points.empty or points.is_empty.any():
        raise ValueError("UTCI requires nonempty tract geometries.")
    columns = np.floor(np.column_stack((points.x, points.y)) / UTCI_GRID + .5) * UTCI_GRID
    columns[:, 0] = (columns[:, 0] + 180) % 360 - 180
    return np.unique(columns, axis=0, return_inverse=True)


def thermal_layer(metric: str, start_date: str, end_date: str, timezone: str = "UTC") -> dict:
    utci = metric == "utci"
    return {
        "label": "UTCI" if utci else "Landsat land-surface temperature",
        "unit": "°C",
        "source": UTCI_SOURCE if utci else LST_SOURCE,
        "source_url": UTCI_URL if utci else "https://www.usgs.gov/landsat-missions/landsat-collection-2-surface-temperature",
        "start_date": start_date,
        "end_date": end_date,
        "resolution": "0.25° native grid" if utci else
        f"{DEFAULT_LST_RESOLUTION_M:g} m analysis grid (30 m Landsat product grid; 100 m native thermal sensor)",
        "aggregation": f"{UTCI_AGGREGATION} Daily boundaries: {timezone}." if utci else
        "Clear-scene median composite; population-weighted where supported, otherwise tract mean.",
        **({"timezone": timezone} if utci else {}),
    }


def load_utci(
    paths: list[Path], tracts: gpd.GeoDataFrame, start_date: str, end_date: str,
    *, timezone: str = "UTC", cds_download: bool = False, cancel_check=lambda: None,
) -> tuple[np.ndarray, int]:
    """Return per-tract °C and the number of distinct source cells sampled."""
    if not paths:
        raise ValueError("Set OASIS_UTCI_PATH to ERA5-HEAT NetCDF data or configure Copernicus CDS access.")
    import xarray as xr

    start = pd.Timestamp(start_date, tz=timezone).tz_convert("UTC")
    end = (pd.Timestamp(end_date) + pd.Timedelta(days=1)).tz_localize(timezone).tz_convert("UTC")
    expected_hours = pd.date_range(start, end, freq="h", inclusive="left")
    centers, tract_indices = utci_cells(tracts)
    longitudes, latitudes = centers.T
    chunks = []
    for path in paths:
        cancel_check()
        with xr.open_dataset(path) as dataset:
            name = next((key for key in ("utci", "universal_thermal_climate_index") if key in dataset), None)
            if name is None:
                raise ValueError("ERA5-HEAT file has no UTCI variable.")
            data = dataset[name]
            aliases = {
                short: full for short, full in (("lat", "latitude"), ("lon", "longitude"))
                if short in data.dims and short in data.coords and full not in data.coords
            }
            data = data.rename(aliases)
            time = next((key for key in ("time", "valid_time") if key in data.dims), None)
            if time is None or set(data.dims) != {time, "latitude", "longitude"}:
                raise ValueError("Expected hourly UTCI on ERA5-HEAT latitude/longitude grid.")
            grid_x = np.round((data.longitude.to_numpy() + 180) % 360 - 180, 6)
            data = data.assign_coords(longitude=grid_x, latitude=np.round(data.latitude.to_numpy(), 6))
            data = data.sel({time: slice(start.tz_localize(None), (end - pd.Timedelta(hours=1)).tz_localize(None))})
            if not data.sizes[time]:
                continue
            data = data.reindex(latitude=np.unique(latitudes), longitude=np.unique(longitudes)).sel(
                latitude=xr.DataArray(latitudes, dims="cell"),
                longitude=xr.DataArray(longitudes, dims="cell"),
            ).transpose(time, "cell")
            unit = str(data.attrs.get("units", "")).strip().casefold()
            # CDS area subsets may omit the variable's units. The requested product is in Kelvin.
            if not unit and cds_download:
                unit = "kelvin"
            values = data.to_numpy()
            if unit in {"k", "kelvin"}:
                values = values - 273.15
            elif unit not in {"degc", "°c", "celsius", "degree_celsius", "degrees_celsius"}:
                raise ValueError("ERA5-HEAT file must identify its UTCI unit as Kelvin or Celsius.")
            chunks.append(pd.DataFrame(values, index=pd.to_datetime(data[time].to_numpy(), utc=True)))
    if not chunks:
        raise ValueError(f"ERA5-HEAT has no hours in {start_date} to {end_date}.")
    hourly = pd.concat(chunks).sort_index()
    if hourly.index.has_duplicates:
        grouped = hourly.groupby(level=0)
        if ((grouped.max() - grouped.min()) > 1e-6).any().any():
            raise ValueError("ERA5-HEAT files provide conflicting UTCI values for the same cell and hour.")
        hourly = grouped.first()
    hourly = hourly.reindex(expected_hours)
    if not np.isfinite(hourly.to_numpy()).all():
        raise ValueError(f"ERA5-HEAT has incomplete hourly or spatial coverage for {start_date} to {end_date} ({timezone}).")
    peak = hourly.groupby(hourly.index.tz_convert(timezone).date).max()
    return peak.mean(axis=0).to_numpy()[tract_indices], len(longitudes)
