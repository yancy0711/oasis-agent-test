"""Folium coverage maps with explicit CRS and study-boundary alignment."""
from __future__ import annotations
from html import escape

import folium
import geopandas as gpd
import pandas as pd

from agent.schemas import SiteSolution
from tools.spatial.alignment import ensure_crs


def _candidate_name(row: pd.Series) -> str | None:
    """Return the first meaningful OSM name without falling back to an ID."""
    for field in ("name", "official_name", "short_name", "brand", "operator"):
        value = row.get(field)
        if isinstance(value, str) and value.strip(): return value.strip()
    return None


def _tract_details(tracts: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, list[str], list[str]]:
    """Build a compact tract layer and tooltip fields from available attributes."""
    fields, aliases = ["GEOID"], ["Census tract:"]
    details = tracts[["GEOID", "geometry"]].copy(); details["GEOID"] = details["GEOID"].astype(str)
    specs = (("total_population", "population", "Population:", 0, 1),
             ("elderly_ratio", "elderly_pct", "Age 65+ (%):", 1, 100),
             ("poverty_rate", "poverty_pct", "Poverty (%):", 1, 100),
             ("heat_exposure", "heat_c", "Heat exposure (\N{DEGREE SIGN}C):", 1, 1))
    for source, target, label, digits, scale in specs:
        if source not in tracts: continue
        details[target] = (pd.to_numeric(tracts[source], errors="coerce") * scale).round(digits)
        fields.append(target); aliases.append(label)
    return details, fields, aliases


def coverage_map(tracts: gpd.GeoDataFrame, candidates: gpd.GeoDataFrame,
                 solution: SiteSolution, indicator: str, out_path: str,
                 boundary: gpd.GeoDataFrame | None = None) -> str:
    """Render aligned heat, tract details, selected named sites, and boundary layers."""
    tracts, candidates = ensure_crs(tracts), ensure_crs(candidates)
    frame = candidates if not candidates.empty else tracts
    minx, miny, maxx, maxy = frame.total_bounds
    m = folium.Map(location=[(miny + maxy) / 2, (minx + maxx) / 2], tiles="cartodbpositron")
    if indicator != "heat_exposure" and indicator in tracts:
        folium.Choropleth(geo_data=tracts[["GEOID", "geometry"]].to_json(), data=tracts,
                          columns=["GEOID", indicator], key_on="feature.properties.GEOID",
                          fill_color="YlGnBu", legend_name=indicator, name=f"Indicator: {indicator}").add_to(m)
    if "heat_exposure" in tracts and tracts["heat_exposure"].notna().any():
        folium.Choropleth(geo_data=tracts[["GEOID", "geometry"]].to_json(), data=tracts,
                          columns=["GEOID", "heat_exposure"], key_on="feature.properties.GEOID",
                          fill_color="YlOrRd", nan_fill_opacity=0, legend_name="Heat exposure (\N{DEGREE SIGN}C)",
                          name="Heat exposure (\N{DEGREE SIGN}C)").add_to(m)
    details, fields, aliases = _tract_details(tracts)
    folium.GeoJson(details.to_json(), name="Census tract details",
                   style_function=lambda _: {"color": "#333", "weight": .45, "fillOpacity": 0},
                   highlight_function=lambda _: {"weight": 1.5, "fillOpacity": .12},
                   tooltip=folium.GeoJsonTooltip(fields=fields, aliases=aliases, sticky=False,
                                                  labels=True, localize=True)).add_to(m)
    if boundary is not None and not boundary.empty:
        admin = ensure_crs(boundary)
        folium.GeoJson(admin.to_json(), name="OSM administrative boundary (includes water)", show=False,
                       style_function=lambda _: {"color": "#2455ff", "weight": 2, "dashArray": "6 6",
                                                 "fillOpacity": 0}).add_to(m)
    selected = candidates[candidates["cand_id"].astype(str).isin(map(str, solution.selected_ids))]
    sites = folium.FeatureGroup(name="Selected candidate sites").add_to(m)
    for _, row in selected.iterrows():
        name = _candidate_name(row); tooltip = f"Selected site: {escape(name)}" if name else None
        folium.Marker([row.geometry.y, row.geometry.x], tooltip=tooltip,
                      icon=folium.Icon(color="blue", icon="star")).add_to(sites)
    m.fit_bounds([[miny, minx], [maxy, maxx]], padding=(20, 20))
    folium.LayerControl(collapsed=False).add_to(m); m.save(out_path)
    return out_path

def heat_exposure_map(tracts: gpd.GeoDataFrame, out_path: str,
                      boundary: gpd.GeoDataFrame | None = None) -> str:
    """Render a standalone tract heat-exposure choropleth for heat-only analysis."""
    tracts = ensure_crs(tracts); minx, miny, maxx, maxy = tracts.total_bounds
    m = folium.Map(location=[(miny + maxy) / 2, (minx + maxx) / 2], tiles="cartodbpositron")
    if "heat_exposure" in tracts and tracts["heat_exposure"].notna().any():
        folium.Choropleth(geo_data=tracts[["GEOID", "geometry"]].to_json(), data=tracts,
                          columns=["GEOID", "heat_exposure"], key_on="feature.properties.GEOID",
                          fill_color="YlOrRd", nan_fill_opacity=0, legend_name="Heat exposure (\N{DEGREE SIGN}C)",
                          name="Heat exposure (\N{DEGREE SIGN}C)").add_to(m)
    details, fields, aliases = _tract_details(tracts)
    folium.GeoJson(details.to_json(), name="Census tract details",
                   style_function=lambda _: {"color": "#333", "weight": .45, "fillOpacity": 0},
                   highlight_function=lambda _: {"weight": 1.5, "fillOpacity": .12},
                   tooltip=folium.GeoJsonTooltip(fields=fields, aliases=aliases, sticky=False,
                                                  labels=True, localize=True)).add_to(m)
    if boundary is not None and not boundary.empty:
        admin = ensure_crs(boundary)
        folium.GeoJson(admin.to_json(), name="OSM administrative boundary (includes water)", show=False,
                       style_function=lambda _: {"color": "#2455ff", "weight": 2, "dashArray": "6 6",
                                                 "fillOpacity": 0}).add_to(m)
    m.fit_bounds([[miny, minx], [maxy, maxx]], padding=(20, 20))
    folium.LayerControl(collapsed=False).add_to(m); m.save(out_path)
    return out_path
