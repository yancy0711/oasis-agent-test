"""US Census ACS 5-year: demand-side demographics.

The LLM never guesses ACS variable codes (the B01001_020E jungle). It speaks
semantic names ('elderly_population') and the VARIABLES table below does the
translation. Register new indicators here only.
"""
from __future__ import annotations
import os
import requests
import pandas as pd
import geopandas as gpd
import numpy as np
import pygris
import hashlib, json
from threading import Lock
from oasis.core.spatial.alignment import ensure_crs, repair_geometry
from oasis.core.retry import with_retry, TransientError, RETRYABLE_STATUS
from oasis.core.cache import atomic_target, atomic_write_text, cache_dir

ACS_BASE = "https://api.census.gov/data/{year}/acs/acs5"

_BLOCK_GROUP_LOCKS: dict[tuple[int, str], Lock] = {}
_BLOCK_GROUP_STATE_CACHE: dict[tuple[int, str], gpd.GeoDataFrame] = {}
_BLOCK_GROUP_LOCK_GUARD = Lock()

# Semantic alias -> ACS variable codes.
VARIABLES: dict[str, dict[str, str]] = {
    "total_population": {"B01003_001E": "total_population"},
    # 65+ must be summed across sex-by-age groups -- classic beginner mistake.
    "elderly_population": {
        "B01001_020E": "m65_66", "B01001_021E": "m67_69", "B01001_022E": "m70_74",
        "B01001_023E": "m75_79", "B01001_024E": "m80_84", "B01001_025E": "m85p",
        "B01001_044E": "f65_66", "B01001_045E": "f67_69", "B01001_046E": "f70_74",
        "B01001_047E": "f75_79", "B01001_048E": "f80_84", "B01001_049E": "f85p",
    },
    "poverty": {"B17001_002E": "below_poverty", "B17001_001E": "poverty_universe"},
    "no_vehicle": {"B08201_002E": "no_vehicle_households", "B08201_001E": "households"},
}


def _validate_table(result) -> list[list[str]]:
    """Reject transient empty/malformed Census payloads before indexing rows."""
    if not isinstance(result, list) or len(result) < 2 or not isinstance(result[0], list):
        raise TransientError("Census API returned no tract rows or a malformed table")
    if not all(isinstance(row, list) and len(row) == len(result[0]) for row in result[1:]):
        raise TransientError("Census API returned rows with inconsistent columns")
    return result


@with_retry(max_retries=4)
def _get(url: str, params: dict) -> list[list[str]]:
    """Fetch and cache one Census API JSON table."""
    public_params = {key: value for key, value in params.items() if key != "key"
    }
    request_key = hashlib.sha256(
            json.dumps({"url": url, "params": public_params,}, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]

    path = cache_dir("census") / f"acs_{request_key}.json"

    if path.exists():
        try:
            return _validate_table(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TransientError):
            path.unlink(missing_ok=True)

    try:
        r = requests.get(url, params=params, timeout=30)
    except requests.RequestException as exc:
        raise TransientError(f"Census API request failed: {exc}") from exc
    if r.status_code in RETRYABLE_STATUS:
        raise TransientError(f"Census API {r.status_code}")
    r.raise_for_status()
    try:
        result = _validate_table(r.json())
    except requests.JSONDecodeError as exc:
        raise TransientError("Census API returned invalid JSON") from exc
    # Cache the result to disk for future calls with the same parameters.
    if not path.exists():
        atomic_write_text(path, json.dumps(result))
    return result


def fetch_acs(state_fips: str, county_fips: str, groups: list[str],
              year: int = 2022) -> pd.DataFrame:
    """Tract-level pull of the requested semantic groups. Returns a wide table
    keyed by GEOID, numeric columns cleaned (ACS encodes NA as -666666666)."""
    codes: dict[str, str] = {}
    for g in groups:
        codes.update(VARIABLES[g])
    rows = _get(ACS_BASE.format(year=year), {
        "get": ",".join(["NAME", *codes]),
        "for": "tract:*",
        "in": f"state:{state_fips} county:{county_fips}",
        "key": os.getenv("CENSUS_API_KEY", ""),
    })
    df = pd.DataFrame(rows[1:], columns=rows[0]).rename(columns=codes)
    df["GEOID"] = df["state"] + df["county"] + df["tract"]
    num = [c for c in df.columns if c not in {"NAME", "state", "county", "tract", "GEOID"}]
    df[num] = df[num].apply(pd.to_numeric, errors="coerce")
    df[num] = df[num].mask(df[num] < 0, np.nan)
    return df


def _state_block_groups(state_fips: str, year: int) -> gpd.GeoDataFrame:
    """Load one state TIGER block-group file once without temporary-file races."""
    key = (int(year), str(state_fips).zfill(2))
    with _BLOCK_GROUP_LOCK_GUARD:
        lock = _BLOCK_GROUP_LOCKS.setdefault(key, Lock())
    with lock:
        cached = _BLOCK_GROUP_STATE_CACHE.get(key)
        if cached is None:
            cached = pygris.block_groups(
                state=key[1], year=key[0], cb=False, cache=True
            )
            if cached is None or cached.empty:
                raise ValueError(f"No block-group geometry returned for state {key[1]}")
            _BLOCK_GROUP_STATE_CACHE[key] = cached
        return cached.copy()


def fetch_block_group_population(
    state_fips: str, county_fips: str, year: int = 2022
) -> gpd.GeoDataFrame:
    """Fetch same-vintage ACS population and TIGER geometry at block-group level."""
    state_fips, county_fips = str(state_fips).zfill(2), str(county_fips).zfill(3)
    rows = _get(ACS_BASE.format(year=year), {
        "get": "NAME,B01003_001E",
        "for": "block group:*",
        "in": f"state:{state_fips} county:{county_fips} tract:*",
        "key": os.getenv("CENSUS_API_KEY", ""),
    })
    population = pd.DataFrame(rows[1:], columns=rows[0]).rename(
        columns={"B01003_001E": "population"}
    )
    population["GEOID"] = (
        population["state"] + population["county"]
        + population["tract"] + population["block group"]
    )
    population["population"] = pd.to_numeric(population["population"], errors="coerce").mask(
        lambda values: values < 0
    )

    path = cache_dir("pygris") / f"block_groups_{year}_{state_fips}_{county_fips}_cb0.gpkg"
    if path.exists():
        geometry = gpd.read_file(path)
    else:
        geometry = _state_block_groups(state_fips, year)
        county_columns = [column for column in geometry if column.upper().startswith("COUNTYFP")]
        if not county_columns:
            raise ValueError(f"Block-group data contains no COUNTYFP column: {list(geometry.columns)}")
        geometry = geometry.loc[
            geometry[county_columns[0]].astype(str).str.zfill(3) == county_fips
        ].copy()
        if geometry.empty:
            raise ValueError(f"No block-group geometry returned for {state_fips}{county_fips}")
        geoid_columns = [column for column in geometry if column.upper().startswith("GEOID")]
        if not geoid_columns:
            raise ValueError(f"Block-group data contains no GEOID column: {list(geometry.columns)}")
        geometry = geometry.rename(columns={geoid_columns[0]: "GEOID"})
        geometry["GEOID"] = geometry["GEOID"].astype(str).str.zfill(12)
        geometry = repair_geometry(ensure_crs(geometry, target="EPSG:4326"))
        with atomic_target(path) as temporary:
            geometry.to_file(temporary, layer="block_groups", driver="GPKG")
    result = geometry.merge(
        population[["GEOID", "population"]], on="GEOID", how="inner", validate="one_to_one"
    )
    if result.empty:
        raise ValueError(f"ACS/TIGER block-group join returned no rows for {state_fips}{county_fips}")
    return result[["GEOID", "population", "geometry"]].reset_index(drop=True)


def derive_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Convert raw ACS counts into numeric ratio indicators."""
    out = df.copy()

    elderly_columns = [
        column
        for column in df.columns
        if column[:2] in {"m6", "m7", "m8", "f6", "f7", "f8"}
    ]

    if elderly_columns:
        elderly_population = (
            df[elderly_columns]
            .apply(pd.to_numeric, errors="coerce")
            .sum(axis=1, min_count=1)
            .astype("float64")
        )

        total_population = pd.to_numeric(
            df["total_population"],
            errors="coerce",
        ).astype("float64")

        total_population = total_population.mask(
            total_population == 0,
            np.nan,
        )

        out["elderly_ratio"] = (
            elderly_population / total_population
        ).astype("float64")
        out["elderly_population"] = elderly_population

    if {"below_poverty", "poverty_universe"} <= set(df.columns):
        below_poverty = pd.to_numeric(
            df["below_poverty"],
            errors="coerce",
        ).astype("float64")

        poverty_universe = pd.to_numeric(
            df["poverty_universe"],
            errors="coerce",
        ).astype("float64")

        poverty_universe = poverty_universe.mask(
            poverty_universe == 0,
            np.nan,
        )

        out["poverty_rate"] = (
            below_poverty / poverty_universe
        ).astype("float64")

    if {"no_vehicle_households", "households"} <= set(df.columns):
        no_vehicle = pd.to_numeric(
            df["no_vehicle_households"], errors="coerce"
        ).astype("float64")
        households = pd.to_numeric(
            df["households"], errors="coerce"
        ).astype("float64").mask(lambda values: values == 0, np.nan)
        out["no_vehicle_ratio"] = (no_vehicle / households).clip(0.0, 1.0)

    return out


def fetch_tract_geometry(state_fips: str, county_fips: str, year: int = 2022) -> gpd.GeoDataFrame:
    """Download and clean Census tract polygons for one county."""
    state_fips = str(state_fips).zfill(2)
    county_fips = str(county_fips).zfill(3)
    # Cache path for the requested tract geometry, keyed by year and FIPS codes.
    cache_path = cache_dir("pygris") / (
        f"tracts_{year}_{state_fips}_{county_fips}_cb0.gpkg"
    )

    # Read from cache if it exists, otherwise fetch from pygris and save to cache.
    if cache_path.exists():
        return gpd.read_file(cache_path)

    gdf = pygris.tracts(
        state=state_fips,
        county=county_fips,
        year=year,
        cb=False,
        cache=False, # close the pygris cache, we manage our own in robustness/cache.py
    )

    if gdf is None or gdf.empty:
        raise ValueError(
            f"No tract geometry returned for "
            f"state={state_fips}, county={county_fips}, year={year}"
        )

    # Different data sources use different GEOID column names, e.g., "GEOID10" or "GEOID20".
    geoid_columns = [
        column
        for column in gdf.columns
        if column.upper().startswith("GEOID")
    ]

    if not geoid_columns:
        raise ValueError(
            f"Tract data contains no GEOID column. "
            f"Available columns: {list(gdf.columns)}"
        )

    source_geoid = geoid_columns[0]

    if source_geoid != "GEOID":
        gdf = gdf.rename(columns={source_geoid: "GEOID"})

    gdf["GEOID"] = (
        gdf["GEOID"]
        .astype(str)
        .str.strip()
        .str.zfill(11)
    )

    # Incase the TIGER data contains tracts from neighboring counties, filter to the expected prefix.
    expected_prefix = state_fips + county_fips
    gdf = gdf[
        gdf["GEOID"].str.startswith(expected_prefix)
    ].copy()

    # TIGERweb ships in EPSG:4269, but we want EPSG:4326 for downstream processing.
    gdf = ensure_crs(gdf, target="EPSG:4326")
    gdf = repair_geometry(gdf)

    if gdf.empty:
        raise ValueError(
            f"No valid tract geometry remained after cleaning for "
            f"{expected_prefix}"
        )

    with atomic_target(cache_path) as temporary:
        gdf.to_file(temporary, layer="tracts", driver="GPKG")

    return gdf.reset_index(drop=True)


def fetch_county_geometry(state_fips: str, year: int = 2022) -> gpd.GeoDataFrame:
    """Download and clean all county polygons for one state."""
    state_fips = str(state_fips).zfill(2)
    path = cache_dir("pygris") / f"counties_{year}_{state_fips}_cb0.gpkg"
    if path.exists(): return gpd.read_file(path)
    gdf = pygris.counties(state=state_fips, year=year, cb=False, cache=False)
    if gdf is None or gdf.empty: raise ValueError(f"No county geometry returned for state={state_fips}, year={year}")
    columns = [column for column in gdf.columns if column.upper().startswith("COUNTYFP")]
    if not columns: raise ValueError(f"County data contains no COUNTYFP column: {list(gdf.columns)}")
    if columns[0] != "COUNTYFP": gdf = gdf.rename(columns={columns[0]: "COUNTYFP"})
    gdf["COUNTYFP"] = gdf["COUNTYFP"].astype(str).str.zfill(3)
    gdf = repair_geometry(ensure_crs(gdf, target="EPSG:4326"))
    if gdf.empty: raise ValueError(f"No valid county geometry remained for state={state_fips}")
    with atomic_target(path) as temporary:
        gdf.to_file(temporary, layer="counties", driver="GPKG")
    return gdf.reset_index(drop=True)
