"""Official Hong Kong census layers and validated local demand data."""
from __future__ import annotations

import hashlib
import json
from math import ceil
from pathlib import Path
import re
from threading import Lock

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
from shapely import make_valid
from shapely.geometry import shape

from oasis.core.cache import CACHE_DIR, PROJECT_ROOT, atomic_write_text
from oasis.core.data import grdi, shenzhen
from oasis.core.data.geocoding import international_boundary_candidates
from oasis.core.spatial.coordinates import normalize_wgs84

HK_SOURCE = "Hong Kong Census and Statistics Department, 2021 Population Census"
HK_CENSUS_URL = "https://data.gov.hk/en-data/dataset/hk-censtatd-census_geo-2021-population-census-by-stpu"
HK_INCOME_FIELD = "low_income_household_share"
_SERVICE = "https://portal.csdi.gov.hk/server/rest/services/common"
_DISTRICT_LAYER = "censtatd_rcd_1635932856094_19982"
_DEMAND_LAYER = "censtatd_rcd_1635933193720_58660"
UPLOAD_ROOT = PROJECT_ROOT / "tmp" / "uploads"
_WORLDPOP_LOCK = Lock()


def _confirmed_path(place: str) -> Path:
    key = hashlib.sha256(place.strip().casefold().encode()).hexdigest()[:20]
    return CACHE_DIR / "regional" / f"confirmed_osm_{key}.json"


def confirm_international_boundary(place: str, row: dict) -> None:
    """Persist a human-confirmed OSM polygon for data checks and the run worker."""
    polygon = make_valid(shape(row["geojson"]))
    if polygon.is_empty or polygon.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError("The selected result is not a usable study-area polygon.")
    atomic_write_text(_confirmed_path(place), json.dumps({
        "geometry": polygon.__geo_interface__,
        "source_url": f"https://www.openstreetmap.org/{row['osm_type']}/{row['osm_id']}",
        "country_code": row["address"]["country_code"],
    }))


def _worldpop_data(place: str) -> tuple[dict, gpd.GeoDataFrame, gpd.GeoDataFrame]:
    confirmed = _confirmed_path(place)
    if not confirmed.is_file():
        raise ValueError("Confirm an administrative boundary before loading regional population.")
    record = json.loads(confirmed.read_text(encoding="utf-8"))
    boundary = gpd.GeoDataFrame(geometry=[shape(record["geometry"])], crs=4326)
    metric = boundary.estimate_utm_crs()
    west, south, east, north = boundary.to_crs(metric).total_bounds
    grid_m = max(1000, ceil(max(east - west, north - south) / 8000) * 1000)
    geometry_key = hashlib.sha256(boundary.geometry.iat[0].wkb).hexdigest()[:12]
    path = confirmed.with_name(f"{confirmed.stem}_{geometry_key}_worldpop_r2025a_{shenzhen.YEAR}_{grid_m}.geojson")
    with _WORLDPOP_LOCK:
        if path.is_file():
            zones = gpd.GeoDataFrame.from_features(json.loads(path.read_text(encoding="utf-8")), crs=4326)
        else:
            zones = shenzhen._zones(boundary.geometry.iat[0], grid_m, crs=metric).clip(boundary, keep_geom_type=True)
            totals, older = [], []
            for start in range(0, len(zones), 20):
                batch_totals, batch_older = shenzhen._population(zones.iloc[start:start + 20])
                totals.extend(batch_totals)
                older.extend(batch_older)
            zones["total_population"], zones["elderly_population"] = totals, older
            zones = zones.loc[zones["total_population"].gt(0)].copy()
            if zones.empty:
                raise ValueError("WorldPop returned no populated zones for the confirmed boundary.")
            atomic_write_text(path, zones.to_json(drop_id=True))
    zones["elderly_population"] = zones["elderly_population"].astype(float)
    metadata = {"source": shenzhen.SOURCE, "source_url": shenzhen.SOURCE_URL, "year": shenzhen.YEAR,
                "notes": f"{record['source_url']}; {grid_m / 1000:g} km grid clipped to the study boundary. "
                         "WorldPop estimated population and age 65+, where available.",
                "timezone": "Asia/Shanghai" if record["country_code"] == "cn" else "UTC"}
    return metadata, boundary, zones

_DISTRICTS = {
    "Central and Western": ("中西區", "中西区"), "Wan Chai": ("灣仔區", "湾仔区", "灣仔", "湾仔"),
    "Eastern": ("東區", "东区"), "Southern": ("南區", "南区"),
    "Yau Tsim Mong": ("油尖旺區", "油尖旺区", "油尖旺"), "Sham Shui Po": ("深水埗區", "深水埗区", "深水埗"),
    "Kowloon City": ("九龍城區", "九龙城区", "九龍城", "九龙城"), "Wong Tai Sin": ("黃大仙區", "黄大仙区", "黃大仙", "黄大仙"),
    "Kwun Tong": ("觀塘區", "观塘区", "觀塘", "观塘"), "Kwai Tsing": ("葵青區", "葵青区", "葵青"),
    "Tsuen Wan": ("荃灣區", "荃湾区", "荃灣", "荃湾"), "Tuen Mun": ("屯門區", "屯门区", "屯門", "屯门"),
    "Yuen Long": ("元朗區", "元朗区", "元朗"), "North": ("北區", "北区"), "Tai Po": ("大埔區", "大埔区", "大埔"),
    "Sha Tin": ("沙田區", "沙田区", "沙田"), "Sai Kung": ("西貢區", "西贡区", "西貢", "西贡"),
    "Islands": ("離島區", "离岛区", "離島", "离岛"),
}


def hong_kong_place(value: str) -> str | None:
    """Resolve supported English and Chinese names without guessing at neighborhoods."""
    query = re.sub(r"\s*[,，]\s*", ", ", value.casefold().strip())
    if query in {"hong kong", "hong kong sar", "hong kong, china", "hong kong sar, china", "hong kong, hong kong",
                 "hk", "hksar", "香港", "香港特別行政區", "香港特别行政区"}:
        return "Hong Kong"
    hk_hint = query.endswith(", hk") or any(name in query for name in ("hong kong", "香港", "kowloon", "九龍", "九龙"))
    for tail in (", china", ", hong kong sar", ", hong kong", ", hk", ", kowloon"):
        query = query.removesuffix(tail).strip()
    query = query.removeprefix("香港").lstrip(" ,").removesuffix("香港").rstrip(" ,").removesuffix(" district")
    for district, aliases in _DISTRICTS.items():
        if query in aliases or (query == district.casefold() and (hk_hint or district not in {"Eastern", "Southern", "North", "Islands"})):
            return f"{district}, Hong Kong"
    return None


def regional_place(value: str) -> str | None:
    if name := hong_kong_place(value):
        return name
    query = re.sub(r"[\s,，]+", "", value.casefold())
    query = query.replace("shenzhencity", "shenzhen").replace("深圳市", "深圳").replace("南山區", "南山区")
    english = {"nanshan", "nanshandistrict"}
    if any(query in {f"{district}shenzhen{suffix}", f"shenzhen{district}{suffix}"}
           for district in english for suffix in ("", "china", "guangdong", "guangdongchina")):
        return shenzhen.NAME
    query = re.sub(r"中国|中國|广东省?|廣東省?", "", query)
    if query in {"深圳南山区", "南山区深圳"}:
        return shenzhen.NAME
    if _confirmed_path(value).is_file():
        return value.strip()
    return None


def socioeconomic_indicator(place: str) -> str | None:
    if hong_kong_place(place):
        return HK_INCOME_FIELD
    if regional_place(place) == shenzhen.NAME:
        return grdi.FIELD
    path = _confirmed_path(place)
    if path.is_file() and json.loads(path.read_text(encoding="utf-8")).get("country_code") == "cn":
        return grdi.FIELD
    return None


def socioeconomic_metadata(field: str) -> dict:
    if field == HK_INCOME_FIELD:
        return {"indicator": field, "source": HK_SOURCE, "source_url": HK_CENSUS_URL,
                "method": "Domestic households with monthly income below HK$10,000 divided by all domestic households in the same small TPU group."}
    return {"indicator": field, "source": grdi.SOURCE, "source_url": grdi.SOURCE_URL, "method": grdi.METHOD}


def automatic_data(place: str) -> tuple[dict, gpd.GeoDataFrame, gpd.GeoDataFrame]:
    if place == shenzhen.NAME:
        boundary, zones = shenzhen.nanshan_data()
        return {"source": shenzhen.SOURCE, "source_url": shenzhen.SOURCE_URL, "year": shenzhen.YEAR,
                "notes": shenzhen.NOTES, "timezone": "Asia/Shanghai"}, boundary, zones
    if hong_kong_place(place):
        boundary, zones = hong_kong_data(place)
        return {"source": HK_SOURCE, "source_url": HK_CENSUS_URL, "year": 2021, "timezone": "Asia/Hong_Kong",
                "notes": "Whole small TPU groups assigned by interior representative point."}, boundary, zones
    return _worldpop_data(place)


def automatic_boundary(place: str) -> tuple[dict, gpd.GeoDataFrame]:
    if place == shenzhen.NAME:
        return {"source": "OpenStreetMap", "source_url": shenzhen.BOUNDARY_URL}, gpd.GeoDataFrame(
            geometry=[shenzhen.nanshan_boundary()], crs=4326)
    if hong_kong_place(place):
        return {"source": HK_SOURCE, "year": 2021}, _hk_boundary(place)
    record = json.loads(_confirmed_path(place).read_text(encoding="utf-8"))
    return {"source": "OpenStreetMap", "source_url": record["source_url"]}, gpd.GeoDataFrame(
        geometry=[shape(record["geometry"])], crs=4326)


def regional_timezone(place: str) -> str:
    if place == shenzhen.NAME:
        return "Asia/Shanghai"
    if hong_kong_place(place):
        return "Asia/Hong_Kong"
    path = _confirmed_path(place)
    return "Asia/Shanghai" if path.is_file() and json.loads(path.read_text(encoding="utf-8"))["country_code"] == "cn" else "UTC"


def _hk_layer(layer: str, columns: str, expected: int) -> gpd.GeoDataFrame:
    path = CACHE_DIR / "regional" / f"{layer}.geojson"
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    cached_fields = (data.get("features") or [{}])[0].get("properties") or {}
    missing_fields = set(columns.casefold().split(",")) - {key.casefold() for key in cached_fields}
    refresh = not data or bool(missing_fields)
    if refresh:
        try:
            response = requests.get(f"{_SERVICE}/{layer}/MapServer/0/query", params={
                "f": "geojson", "where": "1=1", "outFields": columns, "outSR": 4326,
                "returnGeometry": "true", "orderByFields": "OBJECTID", "resultRecordCount": 500,
            }, timeout=(8, 45))
            response.raise_for_status()
            updated = response.json()
            if updated.get("type") != "FeatureCollection" or len(updated.get("features", [])) != expected:
                raise ValueError("The Hong Kong census API did not return the complete 2021 layer.")
            data = updated
        except (ValueError, requests.RequestException):
            if not data or not missing_fields <= {"dh", "dhi_1", "dhi_2", "objectid"}:
                raise
            # The earlier cache schema can still supply boundaries and population offline.
            refresh = False
    if data.get("type") != "FeatureCollection" or len(data.get("features", [])) != expected:
        raise ValueError("The Hong Kong census API did not return the complete 2021 layer.")
    frame = gpd.GeoDataFrame.from_features(data["features"], crs=4326)
    if refresh:
        atomic_write_text(path, json.dumps(data, ensure_ascii=False))
    return frame


def _hk_boundary(place: str) -> gpd.GeoDataFrame:
    districts = _hk_layer(_DISTRICT_LAYER, "dc_eng,OBJECTID", 18)
    districts.geometry = districts.geometry.make_valid()
    if place != "Hong Kong":
        district = place.removesuffix(", Hong Kong")
        district_name = districts["dc_eng"].str.strip().str.casefold().str.replace("&", "and", regex=False)
        districts = districts.loc[district_name.eq(district.casefold())].copy()
        if districts.empty:
            raise ValueError(f"The official 2021 district boundary was not found for {district}.")
    return districts


def hong_kong_data(place: str) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    districts = _hk_boundary(place)
    units = _hk_layer(_DEMAND_LAYER, "stpug,t_pop,age_5,dh,dhi_1,dhi_2,OBJECTID", 211)
    units = units.rename(columns={"stpug": "GEOID", "t_pop": "total_population", "age_5": "elderly_population"})
    units["GEOID"] = units["GEOID"].astype(str)
    units.geometry = units.geometry.make_valid()
    if place != "Hong Kong":
        units = units.loc[units.geometry.representative_point().covered_by(districts.geometry.union_all())].copy()
    for column in ("total_population", "elderly_population"):
        units[column] = pd.to_numeric(units[column], errors="coerce")
    households, below_6000, from_6000 = (
        pd.to_numeric(units[column], errors="coerce") if column in units else
        pd.Series(np.nan, index=units.index)
        for column in ("dh", "dhi_1", "dhi_2")
    )
    low_income = below_6000 + from_6000
    valid = (households.gt(0) & below_6000.ge(0) & from_6000.ge(0) & low_income.le(households)
             & np.isfinite(households) & np.isfinite(low_income))
    units[HK_INCOME_FIELD] = low_income.where(valid).div(households.where(valid))
    units = units.drop(columns=["dh", "dhi_1", "dhi_2"], errors="ignore")
    units = units.loc[units["total_population"].gt(0)].reset_index(drop=True)
    if units.empty:
        raise ValueError("No populated 2021 census groups were found inside the district.")
    return districts, units


def matching_hong_kong_income(place: str, areas: gpd.GeoDataFrame) -> np.ndarray:
    """Keep the census ratio only where an upload preserves the official unit."""
    _, official = hong_kong_data(place)
    matched = areas.to_crs(4326)[["GEOID", "geometry"]].merge(
        official[["GEOID", HK_INCOME_FIELD, "geometry"]], on="GEOID", how="left",
        suffixes=("", "_official"), validate="one_to_one",
    )
    same_polygon = matched.geometry.geom_equals(gpd.GeoSeries(matched["geometry_official"], crs=4326))
    return matched[HK_INCOME_FIELD].where(same_polygon).to_numpy(float)


def validate_regional_upload(place: str, areas: gpd.GeoDataFrame) -> None:
    west, south, east, north = areas.total_bounds
    if place == shenzhen.NAME:
        if not (113.6 <= west <= east <= 114.15 and 22.15 <= south <= north <= 22.8):
            raise ValueError("The uploaded coordinates fall outside the expected Nanshan study area. Check the source CRS.")
        boundary = shenzhen.nanshan_boundary(cached_only=True)
        if boundary is not None and not areas.geometry.representative_point().covered_by(boundary).all():
            raise ValueError("One or more demand zone representative points fall outside the Nanshan boundary.")
        return
    if _confirmed_path(place).is_file():
        _, boundary = automatic_boundary(place)
        if not areas.geometry.representative_point().covered_by(boundary.geometry.union_all()).all():
            raise ValueError("One or more demand zone representative points fall outside the confirmed boundary. Check the source CRS.")
        return
    if not hong_kong_place(place):
        return
    if not (113.7 <= west <= east <= 114.5 and 22.1 <= south <= north <= 22.6):
        raise ValueError("The uploaded coordinates fall outside the Hong Kong study region.")
    if (CACHE_DIR / "regional" / f"{_DISTRICT_LAYER}.geojson").is_file():
        boundary = _hk_boundary(place).geometry.union_all()
        if not areas.geometry.representative_point().covered_by(boundary).all():
            raise ValueError("One or more demand zone representative points fall outside the named Hong Kong study area.")


def parse_demand_upload(content: bytes, fields: dict[str, str], epsg: int,
                        coordinate_system: str = "epsg") -> gpd.GeoDataFrame:
    """Validate mapped polygons and counts, retaining only the fields used by the model."""
    mapped = [column for column in fields.values() if column]
    if not fields.get("id") or not fields.get("population") or len(set(mapped)) != len(mapped):
        raise ValueError("Map the zone ID, total population and optional age 65+ to distinct fields.")
    try:
        data = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("Upload a UTF-8 GeoJSON FeatureCollection.") from None
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        raise ValueError("Upload a GeoJSON FeatureCollection of demand polygons.")
    features = data.get("features")
    if not isinstance(features, list) or not 1 <= len(features) <= 5000:
        raise ValueError("Upload between 1 and 5,000 demand polygons.")
    rows = []
    for index, feature in enumerate(features, 1):
        try:
            props = feature["properties"]
            unit = str(props[fields["id"]]).strip() if props[fields["id"]] is not None else ""
            population = float(props[fields["population"]])
            elderly = float(props[fields["older"]]) if fields.get("older") else np.nan
            geom = make_valid(shape(feature["geometry"]))
        except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
            raise ValueError(f"Feature {index} is missing a mapped field or valid polygon.") from None
        if not unit or not np.isfinite(population) or population < 0:
            raise ValueError(f"Feature {index} needs a unique ID and a non-negative population.")
        if fields.get("older") and (not np.isfinite(elderly) or not 0 <= elderly <= population):
            raise ValueError(f"Feature {index} has a 65+ count outside 0 to total population.")
        if geom.geom_type not in {"Polygon", "MultiPolygon"} or geom.is_empty:
            raise ValueError(f"Feature {index} must be a valid Polygon or MultiPolygon.")
        rows.append({"GEOID": unit, "total_population": population, "elderly_population": elderly, "geometry": geom})
    frame = normalize_wgs84(gpd.GeoDataFrame(rows), coordinate_system, epsg)
    if frame["GEOID"].duplicated().any() or frame["total_population"].sum() <= 0:
        raise ValueError("Polygon IDs must be unique and the total population must be positive.")
    return frame


def stored_demand(upload_id: str, root: Path | None = None) -> tuple[dict, gpd.GeoDataFrame]:
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        raise ValueError("Invalid demand upload ID.")
    path = (root or UPLOAD_ROOT) / f"{upload_id}_demand.geojson"
    if not path.is_file():
        raise ValueError("This demand upload is unavailable. Upload the GeoJSON again.")
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["metadata"], gpd.GeoDataFrame.from_features(data["features"], crs=4326)


def demand_preview(areas: gpd.GeoDataFrame) -> dict:
    """Show only mapped demographics and lightweight shapes in the preview."""
    from oasis.core.outputs.maps import input_layer

    if "GEOID" not in areas:
        return input_layer(areas, "boundary")
    preview = areas.copy()
    preview["elderly_ratio"] = preview["elderly_population"].div(preview["total_population"].replace(0, np.nan))
    return input_layer(preview, "tract")
