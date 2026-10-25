import { useEffect, useRef, useState } from "react";
import maplibregl, { type GeoJSONSource, type Map as MapLibreMap } from "maplibre-gl";
import type { FeatureCollection } from "geojson";
import "maplibre-gl/dist/maplibre-gl.css";
import { useBasemapStyle } from "../result/useBasemapStyle";
import { CATEGORIES, type Origin, type Place, type DisplayRoute, type SearchResult } from "./residentApi";

interface Props {
  selectedId: string | null;
  fastestId: string | null; mode: SearchResult["mode"]; showPlacePopup: boolean;
  picking: boolean; onPickingChange: (picking: boolean) => void;
  origin: Origin | null; places: Place[]; routes: DisplayRoute[];
  onSelect: (id: string) => void; onOrigin: (longitude: number, latitude: number) => void;
}
const EMPTY: FeatureCollection = { type: "FeatureCollection", features: [] };

export function ResidentMap({ origin, places, routes, onSelect, onOrigin, picking, onPickingChange, selectedId, fastestId, mode, showPlacePopup }: Props) {
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const callbacks = useRef({ onSelect, onOrigin, onPickingChange });
  const pickRef = useRef(false);
  const popup = useRef<maplibregl.Popup | null>(null);
  const placePopup = useRef<maplibregl.Popup | null>(null);
  const placeMarkers = useRef(new Map<string, maplibregl.Marker>());
  const [ready, setReady] = useState(false);
  const [notice, setNotice] = useState("");
  const style = useBasemapStyle();
  useEffect(() => { callbacks.current = { onSelect, onOrigin, onPickingChange }; }, [onSelect, onOrigin, onPickingChange]);
  useEffect(() => {
    pickRef.current = picking;
    if (mapRef.current) mapRef.current.getCanvas().style.cursor = picking ? "crosshair" : "";
  }, [picking, ready]);
  useEffect(() => {
    if (!container.current) return;
    let map: MapLibreMap;
    try {
      map = new maplibregl.Map({ container: container.current, style, center: [-98, 39], zoom: 3 });
    } catch {
      setNotice("The interactive map is unavailable. Use the address input and place list to navigate.");
      return;
    }
    mapRef.current = map;
    map.addControl(new maplibregl.NavigationControl(), "top-right");
    map.on("style.load", () => {
      map.addSource("resident-route", { type: "geojson", data: EMPTY });
      map.addLayer({ id: "resident-route-green", type: "line", source: "resident-route",
        filter: ["==", ["get", "variant"], "greener"],
        paint: { "line-color": "#86cfa0", "line-width": 7, "line-opacity": 0.65, "line-dasharray": [2, 2] } });
      map.addLayer({ id: "resident-route-line", type: "line", source: "resident-route",
        filter: ["!=", ["get", "variant"], "greener"],
        paint: { "line-color": ["match", ["get", "variant"], "fastest", "#c65d13", "#2563eb"], "line-width": 5, "line-opacity": 0.95 } });
      map.addLayer({ id: "resident-route-hit", type: "line", source: "resident-route",
        paint: { "line-color": "#2563eb", "line-width": 18, "line-opacity": 0 } });
      setReady(true);
    });
    map.on("click", "resident-route-hit", (event) => {
      if (pickRef.current) return;
      const properties = event.features?.[0]?.properties;
      if (!properties) return;
      const box = document.createElement("div"); box.className = "resident-route-popup";
      const title = document.createElement("strong"); title.textContent = properties.destination_name; box.appendChild(title);
      const details = document.createElement("p");
      details.textContent = `${Number(properties.duration_minutes).toFixed(1)} min / ${Number(properties.distance_km).toFixed(2)} km`;
      box.appendChild(details);
      if (properties.green_score != null) {
        const green = document.createElement("p"); green.textContent = `ORS green index: ${Number(properties.green_score).toFixed(1)} / 10`; box.appendChild(green);
      }
      const note = document.createElement("p"); note.textContent = properties.message; box.appendChild(note);
      popup.current?.remove();
      placePopup.current?.remove();
      popup.current = new maplibregl.Popup().setLngLat(event.lngLat).setDOMContent(box).addTo(map);
    });
    map.on("mouseenter", "resident-route-hit", () => { if (!pickRef.current) map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", "resident-route-hit", () => { if (!pickRef.current) map.getCanvas().style.cursor = ""; });
    map.on("error", () => setNotice("Some map imagery is unavailable. Place markers, routes and the result list remain available."));
    map.on("click", (event) => {
      if (!pickRef.current) return;
      pickRef.current = false; callbacks.current.onPickingChange(false); map.getCanvas().style.cursor = "";
      callbacks.current.onOrigin(event.lngLat.lng, event.lngLat.lat);
    });
    const observer = new ResizeObserver(() => map.resize());
    observer.observe(container.current);
    return () => { observer.disconnect(); popup.current?.remove(); placePopup.current?.remove(); map.remove(); mapRef.current = null; setReady(false); };
  }, [style]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    const markers: maplibregl.Marker[] = [];
    const registry = placeMarkers.current;
    if (origin) {
      const element = document.createElement("div");
      const outside = !!origin.country_code && origin.country_code !== "us";
      element.className = `resident-origin${outside ? " resident-origin--outside" : ""}`;
      element.setAttribute("role", "img");
      element.setAttribute("aria-label", outside ? "Location outside the United States" : "Starting point");
      element.title = origin.label;
      const dot = document.createElement("span"); element.appendChild(dot);
      markers.push(new maplibregl.Marker({ element }).setLngLat([origin.longitude, origin.latitude]).addTo(map));
    }
    for (const [index, place] of places.entries()) {
      const element = document.createElement("button");
      element.type = "button"; element.className = "resident-map-place";
      element.style.background = CATEGORIES[place.category].color;
      element.textContent = String(index + 1);
      element.title = `${place.name} / ${CATEGORIES[place.category].label} / ${place.duration_minutes.toFixed(1)} min`;
      element.setAttribute("aria-label", `Directions to ${place.name} on map`);
      element.addEventListener("click", (event) => { event.stopPropagation(); callbacks.current.onSelect(place.id); });
      const marker = new maplibregl.Marker({ element }).setLngLat([place.longitude, place.latitude]).addTo(map);
      markers.push(marker); registry.set(place.id, marker);
    }
    if (origin) {
      if (places.length) {
        const bounds = new maplibregl.LngLatBounds([origin.longitude, origin.latitude], [origin.longitude, origin.latitude]);
        places.forEach((place) => bounds.extend([place.longitude, place.latitude]));
        map.fitBounds(bounds, { padding: 65, maxZoom: 15, duration: 700 });
      } else {
        map.flyTo({ center: [origin.longitude, origin.latitude], zoom: origin.country_code === "us" ? 14 : 5, duration: 1100 });
      }
    }
    return () => { markers.forEach((marker) => marker.remove()); registry.clear(); };
  }, [origin, places, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !ready) return;
    popup.current?.remove();
    const data: FeatureCollection = { type: "FeatureCollection", features: routes.flatMap(({ route, variant }) =>
      route.features.map((feature) => ({ ...feature, properties: { ...route.summary, variant } }))) };
    (map.getSource("resident-route") as GeoJSONSource | undefined)?.setData(data);
    // A DOM label also works with the offline/raster style, without font tiles.
    let fastestLabel: maplibregl.Marker | undefined;
    const fastest = data.features.find((feature) => feature.properties?.variant === "fastest");
    if (fastest?.geometry.type === "LineString") {
      const coordinates = fastest.geometry.coordinates;
      const middle = (coordinates.length - 1) / 2;
      const before = coordinates[Math.floor(middle)], after = coordinates[Math.ceil(middle)];
      if (before && after) {
        const element = document.createElement("span");
        element.className = "resident-fastest-route-label";
        element.textContent = "Fastest";
        element.title = "Fastest among the compared destination routes";
        fastestLabel = new maplibregl.Marker({ element }).setLngLat([(before[0] + after[0]) / 2, (before[1] + after[1]) / 2]).addTo(map);
      }
    }
    const bounds = new maplibregl.LngLatBounds();
    for (const feature of data.features) {
      if (feature.geometry.type === "LineString") feature.geometry.coordinates.forEach((coordinate) => bounds.extend([coordinate[0], coordinate[1]]));
    }
    if (!bounds.isEmpty()) {
      const fit = () => map.fitBounds(bounds, { padding: 65, maxZoom: 16, duration: 0 });
      fit(); map.on("resize", fit);
      return () => { map.off("resize", fit); fastestLabel?.remove(); };
    }
    return () => fastestLabel?.remove();
  }, [routes, ready]);

  useEffect(() => {
    const map = mapRef.current;
    for (const [id, marker] of placeMarkers.current) {
      marker.getElement().classList.toggle("is-selected", id === selectedId);
      marker.getElement().setAttribute("aria-pressed", String(id === selectedId));
    }
    const place = places.find((item) => item.id === selectedId);
    placePopup.current?.remove();
    if (map && ready && place && showPlacePopup) {
      const box = document.createElement("div"); box.className = "resident-place-popup";
      const name = document.createElement("strong"); name.textContent = place.name;
      const details = document.createElement("p");
      details.textContent = `${CATEGORIES[place.category].label} · ${place.duration_minutes.toFixed(1)} min ${mode}`;
      box.append(name, details);
      if (place.id === fastestId) {
        const badge = document.createElement("span"); badge.className = "resident-fastest-badge";
        badge.textContent = "Fastest"; badge.title = "Fastest among the compared destination routes"; box.append(badge);
      }
      placePopup.current = new maplibregl.Popup({ offset: 18, maxWidth: "240px" })
        .setLngLat([place.longitude, place.latitude]).setDOMContent(box).addTo(map);
    }
    if (map && ready && place && !routes.length) map.flyTo({ center: [place.longitude, place.latitude], zoom: Math.max(map.getZoom(), 15), duration: 400 });
  }, [selectedId, places, ready, routes, fastestId, mode, showPlacePopup]);

  return <div className="resident-map-panel">
    {notice && <p className="notice">{notice}</p>}
    {places.length > 0 && <div className="resident-map-legend">{[...new Set(places.map((place) => place.category))].map((category) =>
      <span key={category}><span className="resident-category-dot" style={{ background: CATEGORIES[category].color }} />{CATEGORIES[category].label}</span>)}</div>}
    {routes.length > 0 && <div className="resident-route-legend">{routes.map(({ route, variant }) =>
      <span key={variant} title={variant === "fastest" ? "Fastest among the compared destination routes" : undefined}>
        <span className={`resident-route-swatch resident-route-swatch--${variant}`} />
        {variant === "fastest" ? "Fastest" : variant === "greener" ? "Greener compared route" : "Selected route"}: {route.summary.destination_name}
      </span>)}</div>}
    <div className="resident-map-frame">
      <div ref={container} className="resident-map" aria-label="Nearby places map" />
      <div className="resident-map-tools">
        <button type="button" disabled={!ready} aria-pressed={picking} onClick={() => onPickingChange(!picking)}>{picking ? "Cancel map selection" : "Select starting point on map"}</button>
        {picking && <p role="status">Click a point on the map, then confirm it to search.</p>}
      </div>
    </div>

  </div>;
}
