"""Download and cache official regional ERA5-HEAT data."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from zipfile import ZipFile, is_zipfile

import pandas as pd

from oasis.core.data.thermal import UTCI_GRID, utci_cells
from oasis.core.cache import CACHE_DIR


def cds_configured() -> bool:
    if importlib.util.find_spec("cdsapi") is None:
        return False
    config = Path(os.getenv("CDSAPI_RC") or Path.home() / ".cdsapirc")
    return bool(os.getenv("CDSAPI_KEY") or config.is_file())


def regional_boxes(tracts, *, margin: float = 0.01) -> list[list[float]]:
    centers, _ = utci_cells(tracts)
    longitudes = centers[:, 0]
    groups = [centers] if longitudes.max() - longitudes.min() <= 180 else [centers[longitudes < 0], centers[longitudes >= 0]]
    return [
        [min(90, round(float(group[:, 1].max()) + margin, 4)),
         max(-180, round(float(group[:, 0].min()) - margin, 4)),
         max(-90, round(float(group[:, 1].min()) - margin, 4)),
         min(180, round(float(group[:, 0].max()) + margin, 4))]
        for group in groups
    ]


def regional_utci_files(tracts, start_date, end_date, timezone, *, progress, cancel_check) -> list[Path]:
    start = pd.Timestamp(start_date, tz=timezone).tz_convert("UTC")
    end = (pd.Timestamp(end_date) + pd.Timedelta(days=1)).tz_localize(timezone).tz_convert("UTC")
    dates = pd.date_range(start.floor("D"), (end - pd.Timedelta(hours=1)).floor("D"), freq="D")
    return download_utci(regional_boxes(tracts), dates, CACHE_DIR / "era5-heat",
                         progress=progress, cancel_check=cancel_check, legacy_boxes=regional_boxes(tracts, margin=UTCI_GRID))


def _cached_files(cache_dir: Path, request: dict) -> tuple[Path, list[Path]]:
    key = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()[:24]
    target = cache_dir / key
    return target, sorted(target.glob("*.nc"))


def download_utci(boxes, dates, cache_dir, *, progress, cancel_check, client=None, legacy_boxes=None) -> list[Path]:
    dates = dates.tz_localize(None)
    requests = []
    for box_index, area in enumerate(boxes):
        for month in dates.to_period("M").unique():
            days = dates[(dates.year == month.year) & (dates.month == month.month)]
            requests.append(({
                "variable": ["universal_thermal_climate_index"], "version": "1_1",
                "product_type": "consolidated_dataset", "year": [str(month.year)],
                "month": [f"{month.month:02d}"], "day": [f"{day.day:02d}" for day in days],
                "area": area,
            }, legacy_boxes[box_index] if legacy_boxes else None))
    cache_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for index, (request, legacy_area) in enumerate(requests):
        cancel_check()
        target, existing = _cached_files(cache_dir, request)
        if not existing and legacy_area:
            _, existing = _cached_files(cache_dir, {**request, "area": legacy_area})
        if existing:
            progress("Reusing cached regional ERA5-HEAT", index + 1, len(requests))
        if not existing:
            if client is None:
                import cdsapi
                if os.getenv("CDSAPI_KEY"):
                    client = cdsapi.Client(url=os.getenv("CDSAPI_URL") or "https://cds.climate.copernicus.eu/api",
                                           timeout=90, retry_max=3)
                else:
                    client = cdsapi.Client(timeout=90, retry_max=3)
            month = f"{request['year'][0]}-{request['month'][0]}"
            progress(f"Downloading regional ERA5-HEAT · {month}", index, len(requests))
            with TemporaryDirectory(dir=cache_dir) as staging:
                archive = Path(staging) / "response"
                extracted = Path(staging) / "netcdf"
                extracted.mkdir()
                client.retrieve("derived-utci-historical", request).download(str(archive))
                cancel_check()
                if is_zipfile(archive):
                    with ZipFile(archive) as zipped:
                        for member in zipped.infolist():
                            filename = Path(member.filename).name
                            if not member.is_dir() and Path(filename).suffix.lower() == ".nc":
                                with zipped.open(member) as source, (extracted / filename).open("xb") as output:
                                    shutil.copyfileobj(source, output)
                else:
                    with archive.open("rb") as source:
                        if not source.read(8).startswith((b"CDF", b"\x89HDF\r\n\x1a\n")):
                            raise ValueError("Copernicus did not return NetCDF data.")
                    archive.replace(extracted / "utci.nc")
                if not any(extracted.glob("*.nc")):
                    raise ValueError("The Copernicus archive contained no UTCI NetCDF files.")
                try:
                    extracted.rename(target)
                except OSError:
                    if not any(target.glob("*.nc")):
                        raise
            existing = sorted(target.glob("*.nc"))
        files.extend(existing)
    progress("Regional ERA5-HEAT files ready", len(requests), len(requests))
    return files
