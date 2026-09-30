import { useCallback, useEffect, useRef, useState, type FormEvent, type PointerEvent as ReactPointerEvent } from "react";
import type { Feature, FeatureCollection, Point } from "geojson";
import maplibregl, { type GeoJSONSource, type Map as MapLibreMap } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useBasemapStyle } from "./useBasemapStyle";
import { planPlaceRoute, planPointRoute, type PlannedRoute } from "../../api/client";
import { HEAT_COLORS, MAP_LAYER_IDS, MAP_SOURCE_ID, PARETO_SOURCE_ID, ROUTE_SOURCE_ID, paretoPreviewLayer, plannedRouteLayers, resultLayers } from "./mapConfig";
import { popupContent } from "./mapPopup";
import type { ParetoPoint } from "./ParetoView";
import { useMediaQuery } from "../../hooks/useMediaQuery";

interface MapMetadata {
  renderer: "maplibre";
  indicator: string;
  indicator_label: string;
  value_range: [number, number] | null;
  bounds: [[number, number], [number, number]];
}
interface MapDataset extends FeatureCollection { metadata: MapMetadata; }
interface MapViewProps { url?: string; runId: string; paretoPreview?: ParetoPoint | null; onClearParetoPreview?: () => void; showRoutePlanner?: boolean; focusTractId?: string | null; routeOriginQuery?: string | null; routeOriginCoordinate?: { longitude: number; latitude: number } | null; }
type LayerState = { tracts: boolean; boundary: boolean; route: boolean; sites: boolean };
type RouteMode = "walking" | "cycling" | "driving";
type PanelPosition = { x: number; y: number };

const EMPTY_ROUTE: FeatureCollection = { type: "FeatureCollection", features: [] };
const DEFAULT_ROUTE_POSITION: PanelPosition = { x: 10, y: 58 };

function indicatorLabel(label: string): string {
  return /^Heat exposure\s*\(/i.test(label) ? "Heat exposure (°C)" : label;
}

function RouteAudit({ summary }: { summary: Record<string, unknown> }) {
  if (!summary.duration_minutes) return null;
  const duration = Number(summary.duration_minutes);
  const heat = summary.route_mean_lst_c == null ? null : Number(summary.route_mean_lst_c);
  const dose = summary.surface_temperature_minutes == null ? null : Number(summary.surface_temperature_minutes);
  const calculation = heat == null || dose == null ? null
    : `${heat.toFixed(1)} °C × ${duration.toFixed(1)} min = ${dose.toFixed(0)} °C·min`;
  return <section className="route-audit"><h4>Active route heat audit</h4><div><span>Network travel</span><strong>{duration.toFixed(1)} min</strong></div><div><span>Route mean LST</span><strong>{heat == null ? "Unavailable" : `${heat.toFixed(1)} °C`}</strong></div><div><span>LST × travel-time index</span><strong>{dose == null ? "Unavailable" : `${dose.toFixed(0)} °C·min`}</strong></div><p>{calculation ? `${calculation}. ` : ""}This is a comparative route index, not a temperature reading or physiological heat dose. It does not alter ORS routing or facility optimization.</p></section>;
}

export function MapView({ url, runId, paretoPreview, onClearParetoPreview, showRoutePlanner = true, focusTractId = null, routeOriginQuery = null, routeOriginCoordinate = null }: MapViewProps) {
  const baseMapStyle = useBasemapStyle();
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const routeControlRef = useRef<HTMLDivElement>(null);
  const layersControlRef = useRef<HTMLDetailsElement>(null);
  const dragRef = useRef<{ pointerId: number; offsetX: number; offsetY: number } | null>(null);
  const routeManuallyMovedRef = useRef(false);
  const planAtRef = useRef<((longitude: number, latitude: number) => void) | null>(null);
  const autoRouteRef = useRef<string | null>(null);
  const outsideReturnTimerRef = useRef<number | null>(null);
  const planningEnabledRef = useRef(false);
  const routeModeRef = useRef<RouteMode>("walking");
  const mobile = useMediaQuery("(max-width: 620px)");
  const [data, setData] = useState<MapDataset | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mapReady, setMapReady] = useState(false);
  const [bearing, setBearing] = useState(0);
  const [planningEnabled, setPlanningEnabled] = useState(false);
  const [routeMode, setRouteMode] = useState<RouteMode>("walking");
  const [routeStatus, setRouteStatus] = useState('Select “Plan from map point”, then click an exact origin.');
  const [routeBusy, setRouteBusy] = useState(false);
  const [routeAudit, setRouteAudit] = useState<Record<string, unknown> | null>(null);
  const [originQuery, setOriginQuery] = useState("");
  const [routeCollapsed, setRouteCollapsed] = useState(mobile);
  const [routePosition, setRoutePosition] = useState<PanelPosition>(DEFAULT_ROUTE_POSITION);
  const [layers, setLayers] = useState<LayerState>({ tracts: true, boundary: true, route: true, sites: true });

  useEffect(() => { planningEnabledRef.current = planningEnabled; if (mapRef.current) mapRef.current.getCanvas().style.cursor = planningEnabled ? "crosshair" : ""; }, [planningEnabled]);
  useEffect(() => { routeModeRef.current = routeMode; }, [routeMode]);
  useEffect(() => {
    const clampPanel = () => {
      const panel = routeControlRef.current;
      const container = containerRef.current;
      if (!panel || !container) return;
      const bounds = container.getBoundingClientRect();
      const panelRect = panel.getBoundingClientRect();
      setRoutePosition((current) => ({
        x: mobile && !routeManuallyMovedRef.current
          ? Math.max(6, (bounds.width - panelRect.width) / 2)
          : Math.max(6, Math.min(current.x, bounds.width - panelRect.width - 6)),
        y: Math.max(6, Math.min(current.y, bounds.height - panelRect.height - 6)),
      }));
    };
    clampPanel();
    const observer = new ResizeObserver(clampPanel);
    if (routeControlRef.current) observer.observe(routeControlRef.current);
    window.addEventListener("resize", clampPanel);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", clampPanel);
    };
  }, [mapReady, mobile, routeCollapsed]);

  const routeResultStatus = (result: PlannedRoute) => {
    const summary = result.summary;
    const origin = summary.origin_label ? `From ${summary.origin_label}. ` : "";
    return `${origin}Nearest selected candidate facility: ${summary.destination_name ?? summary.destination_id}; ${Number(summary.duration_minutes).toFixed(1)} min by ${summary.mode}.`;
  };

  const showRoute = useCallback((result: PlannedRoute, longitude: number, latitude: number) => {
    const map = mapRef.current;
    const source = map?.getSource(ROUTE_SOURCE_ID) as GeoJSONSource | undefined;
    if (!map || !source) throw new Error("The interactive map is not ready.");
    if (outsideReturnTimerRef.current !== null) {
      window.clearTimeout(outsideReturnTimerRef.current);
      outsideReturnTimerRef.current = null;
    }
    const origin: Feature<Point> = {
      type: "Feature", geometry: { type: "Point", coordinates: [longitude, latitude] },
      properties: { layer_kind: "route_origin" },
    };
    source.setData({ type: "FeatureCollection", features: [...result.features, origin] });
    map.easeTo({ center: [longitude, latitude], duration: 450 });
    setRouteStatus(routeResultStatus(result));
    setRouteAudit(result.summary);
    setPlanningEnabled(false);
  }, []);

  const showRouteResult = useCallback((result: PlannedRoute, longitude: number, latitude: number) => {
    if (result.summary.status !== "outside_study_area") {
      showRoute(result, longitude, latitude);
      return;
    }
    const map = mapRef.current;
    const source = map?.getSource(ROUTE_SOURCE_ID) as GeoJSONSource | undefined;
    if (!map || !source || !data) throw new Error("The interactive map is not ready.");
    if (outsideReturnTimerRef.current !== null) window.clearTimeout(outsideReturnTimerRef.current);
    source.setData(result);
    map.flyTo({ center: [longitude, latitude], zoom: 11, duration: 650 });
    setRouteStatus(String(result.summary.message ?? "This origin is outside the current study area."));
    setRouteAudit(null);
    setPlanningEnabled(false);
    outsideReturnTimerRef.current = window.setTimeout(() => {
      map.fitBounds(data.metadata.bounds, { padding: 30, duration: 700, maxZoom: 13 });
      source.setData(EMPTY_ROUTE);
      outsideReturnTimerRef.current = null;
    }, 3000);
  }, [data, showRoute]);

  useEffect(() => () => {
    if (outsideReturnTimerRef.current !== null) window.clearTimeout(outsideReturnTimerRef.current);
  }, []);

  useEffect(() => {
    setOriginQuery(routeOriginQuery ?? "");
    autoRouteRef.current = null;
  }, [routeOriginQuery, runId, url]);

  useEffect(() => {
    const query = routeOriginQuery?.trim();
    const coordinate = routeOriginCoordinate;
    const autoRouteKey = `${runId}:${url ?? ""}:${coordinate ? `${coordinate.longitude},${coordinate.latitude}` : query}`;
    if (!mapReady || !showRoutePlanner || (!query && !coordinate) || autoRouteRef.current === autoRouteKey) return;
    autoRouteRef.current = autoRouteKey;
    setRouteBusy(true);
    setRouteStatus(coordinate
      ? "Planning from the confirmed browser location..."
      : `Finding "${query}" and planning its nearest selected facility...`);
    const request = coordinate
      ? planPointRoute(runId, coordinate.longitude, coordinate.latitude, routeModeRef.current)
      : planPlaceRoute(runId, query as string, routeModeRef.current);
    request.then((result) => {
      if (autoRouteRef.current !== autoRouteKey) return;
      const longitude = coordinate?.longitude ?? Number(result.summary.origin_longitude);
      const latitude = coordinate?.latitude ?? Number(result.summary.origin_latitude);
      if (!Number.isFinite(longitude) || !Number.isFinite(latitude)) throw new Error("The geocoder returned an invalid origin coordinate.");
      showRouteResult(result, longitude, latitude);
    }).catch((reason: unknown) => {
      if (autoRouteRef.current === autoRouteKey) {
        setRouteStatus(reason instanceof Error ? reason.message : "Could not plan from this location.");
      }
    }).finally(() => {
      if (autoRouteRef.current === autoRouteKey) setRouteBusy(false);
    });
  }, [mapReady, routeOriginCoordinate, routeOriginQuery, runId, showRoutePlanner, showRouteResult, url]);

  useEffect(() => {
    if (!url) { setData(null); return; }
    const controller = new AbortController();
    setError(null);
    fetch(url, { signal: controller.signal }).then((response) => {
      if (!response.ok) throw new Error(`Map data request failed (${response.status})`);
      return response.json() as Promise<MapDataset>;
    }).then((payload) => {
      if (payload.type !== "FeatureCollection" || payload.metadata?.renderer !== "maplibre") throw new Error("The map artifact is not valid OASIS MapLibre data.");
      setData(payload);
    }).catch((reason: unknown) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Could not load map data."); });
    return () => controller.abort();
  }, [url]);

  useEffect(() => {
    if (!data || !containerRef.current) return;
    setMapReady(false);
    const map = new maplibregl.Map({ container: containerRef.current, style: baseMapStyle, attributionControl: false });
    mapRef.current = map;
    const mapSizeObserver = new ResizeObserver(() => map.resize());
    mapSizeObserver.observe(containerRef.current);
    map.addControl(new maplibregl.NavigationControl({ showCompass: true, visualizePitch: true }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");
    map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");
    const updateBearing = () => setBearing(map.getBearing());
    map.on("rotate", updateBearing);
    let popup: maplibregl.Popup | null = null;

    map.on("load", () => {
      map.addSource(MAP_SOURCE_ID, { type: "geojson", data });
      map.addSource(ROUTE_SOURCE_ID, { type: "geojson", data: EMPTY_ROUTE });
      map.addSource(PARETO_SOURCE_ID, { type: "geojson", data: EMPTY_ROUTE });
      resultLayers(data.metadata.value_range).forEach((layer) => map.addLayer(layer));
      plannedRouteLayers().forEach((layer) => map.addLayer(layer));
      map.addLayer(paretoPreviewLayer);
      map.fitBounds(data.metadata.bounds, { padding: 30, duration: 0, maxZoom: 13 });
      setMapReady(true);

      planAtRef.current = (longitude, latitude) => {
        const origin: Feature<Point> = { type: "Feature", geometry: { type: "Point", coordinates: [longitude, latitude] }, properties: { layer_kind: "route_origin" } };
        (map.getSource(ROUTE_SOURCE_ID) as GeoJSONSource).setData({ type: "FeatureCollection", features: [origin] });
        setRouteBusy(true);
        setRouteStatus("Origin selected. Comparing network travel times to selected candidate facilities…");
        planPointRoute(runId, longitude, latitude, routeModeRef.current).then((result) => {
          showRouteResult(result, longitude, latitude);
        }).catch((reason: unknown) => {
          setRouteStatus(reason instanceof Error ? reason.message : "Could not plan this route.");
        }).finally(() => setRouteBusy(false));
      };

      let pinnedFeature: string | null = null;
      const showPopup = (event: Pick<maplibregl.MapMouseEvent, "lngLat"> & { features?: maplibregl.MapGeoJSONFeature[] }, closeButton = false, key: string | null = null) => {
      const feature = event.features?.[0]; if (!feature) return;
      popup?.remove();
      pinnedFeature = key;
        if (closeButton) map.easeTo({ center: event.lngLat, duration: 350 });
        const point = feature.geometry.type === "Point" ? feature.geometry.coordinates : [event.lngLat.lng, event.lngLat.lat];
        const coordinates: [number, number] = [Number(point[0]), Number(point[1])];
        const nextPopup = new maplibregl.Popup({ closeButton, closeOnClick: false, offset: 10 }).setLngLat(event.lngLat).setDOMContent(popupContent(feature.properties, coordinates, closeButton)).addTo(map);
        popup = nextPopup;
        nextPopup.on("close", () => { if (popup === nextPopup) { popup = null; pinnedFeature = null; } });
      };
      if (focusTractId) {
        const target = data.features.find((feature) =>
          String(feature.properties?.GEOID ?? "") === focusTractId
        );
        if (target && target.geometry.type !== "Point" && "coordinates" in target.geometry) {
          const bounds = new maplibregl.LngLatBounds();
          const extend = (value: unknown): void => {
            if (!Array.isArray(value)) return;
            if (value.length >= 2 && typeof value[0] === "number" && typeof value[1] === "number") {
              bounds.extend([value[0], value[1]]);
            } else value.forEach(extend);
          };
          extend(target.geometry.coordinates);
          if (!bounds.isEmpty()) {
            map.fitBounds(bounds, { padding: 90, duration: 500, maxZoom: 14 });
            const center = bounds.getCenter();
            showPopup(
              { lngLat: center, features: [target as unknown as maplibregl.MapGeoJSONFeature] },
              true, `tract:${focusTractId}`,
            );
          }
        }
      }
      map.on("mousemove", MAP_LAYER_IDS.tracts, (event) => { if (!pinnedFeature && !planningEnabledRef.current) showPopup(event); });
      map.on("mouseleave", MAP_LAYER_IDS.tracts, () => { if (!pinnedFeature) { popup?.remove(); popup = null; } });
      const interactiveLayers = [MAP_LAYER_IDS.tracts, MAP_LAYER_IDS.sites, MAP_LAYER_IDS.paretoSites, MAP_LAYER_IDS.route];
      map.on("click", (event) => {
        if (planningEnabledRef.current) {
          popup?.remove(); pinnedFeature = null;
          planAtRef.current?.(event.lngLat.lng, event.lngLat.lat);
          return;
        }
        if (pinnedFeature) {
          pinnedFeature = null; popup?.remove(); popup = null;
          return;
        }
        const features = map.queryRenderedFeatures(event.point, { layers: interactiveLayers });
        const feature = features.find((item) => item.layer.id === MAP_LAYER_IDS.sites)
          ?? features.find((item) => item.layer.id === MAP_LAYER_IDS.paretoSites)
          ?? features.find((item) => item.layer.id === MAP_LAYER_IDS.route)
          ?? features.find((item) => item.layer.id === MAP_LAYER_IDS.tracts);
        if (!feature) return;
        const properties = feature.properties ?? {};
        const prefix = feature.layer.id === MAP_LAYER_IDS.tracts ? "tract"
          : feature.layer.id === MAP_LAYER_IDS.route ? "route"
            : feature.layer.id === MAP_LAYER_IDS.paretoSites ? "preview" : "site";
        const id = prefix === "tract" ? properties.GEOID ?? feature.id
          : prefix === "route" ? properties.destination_id : properties.candidate_id;
        showPopup({ ...event, features: [feature] }, true, `${prefix}:${String(id ?? "unknown")}`);
      });
      [MAP_LAYER_IDS.tracts, MAP_LAYER_IDS.sites, MAP_LAYER_IDS.paretoSites, MAP_LAYER_IDS.route].forEach((id) => {
        map.on("mouseenter", id, () => { if (!planningEnabledRef.current) map.getCanvas().style.cursor = "pointer"; });
        map.on("mouseleave", id, () => { map.getCanvas().style.cursor = planningEnabledRef.current ? "crosshair" : ""; });
      });
    });
    return () => {
      if (outsideReturnTimerRef.current !== null) {
        window.clearTimeout(outsideReturnTimerRef.current);
        outsideReturnTimerRef.current = null;
      }
      mapSizeObserver.disconnect(); planAtRef.current = null; popup?.remove();
      map.off("rotate", updateBearing); map.remove(); mapRef.current = null;
      setMapReady(false); setBearing(0);
    };
  }, [data, focusTractId, runId, showRouteResult, baseMapStyle]);

  useEffect(() => {
    const source = mapRef.current?.getSource(PARETO_SOURCE_ID) as GeoJSONSource | undefined;
    if (!source) return;
    source.setData({ type: "FeatureCollection", features: (paretoPreview?.selected_sites ?? []).map((site) => ({
      type: "Feature", geometry: { type: "Point", coordinates: [site.longitude, site.latitude] },
      properties: { layer_kind: "pareto_preview_site", name: site.name, candidate_id: site.id, alpha: paretoPreview?.alpha },
    })) });
  }, [paretoPreview, mapReady]);

  useEffect(() => {
    const map = mapRef.current; if (!mapReady || !map?.isStyleLoaded()) return;
    const visibility = (visible: boolean) => visible ? "visible" : "none";
    [MAP_LAYER_IDS.tracts, MAP_LAYER_IDS.tractOutline].forEach((id) => map.setLayoutProperty(id, "visibility", visibility(layers.tracts)));
    map.setLayoutProperty(MAP_LAYER_IDS.boundary, "visibility", visibility(layers.boundary));
    map.setLayoutProperty(MAP_LAYER_IDS.sites, "visibility", visibility(layers.sites));
    [MAP_LAYER_IDS.route, MAP_LAYER_IDS.routeOrigin].forEach((id) => map.setLayoutProperty(id, "visibility", visibility(layers.route)));
  }, [layers, mapReady]);

  const toggleMapPlanning = () => {
    const next = !planningEnabledRef.current;
    planningEnabledRef.current = next;
    setPlanningEnabled(next);
    if (mapRef.current) mapRef.current.getCanvas().style.cursor = next ? "crosshair" : "";
    setRouteStatus(next
      ? "Selection active: click an exact origin inside a displayed tract."
      : 'Select “Plan from map point”, then click an exact origin.');
  };

  const useLocation = () => {
    if (!navigator.geolocation) {
      setRouteStatus("This browser does not provide geolocation. Enter an address instead.");
      return;
    }
    setRouteStatus("Waiting for browser location permission…"); setRouteBusy(true);
    navigator.geolocation.getCurrentPosition(
      ({ coords }) => {
        setRouteBusy(false);
        setRouteStatus(`Location received (${coords.latitude.toFixed(5)}, ${coords.longitude.toFixed(5)}). Checking whether it is inside the study area…`);
        planAtRef.current?.(coords.longitude, coords.latitude);
      },
      (reason) => {
        const message = reason.code === 1
          ? "Location permission was denied. Allow location access or enter an address."
          : reason.code === 2
            ? "The browser could not determine your location. Enter an address instead."
            : reason.code === 3
              ? "Location lookup timed out. Try again or enter an address."
              : `Location lookup failed: ${reason.message || "unknown browser error"}`;
        setRouteBusy(false); setRouteStatus(message);
      },
      { enableHighAccuracy: true, timeout: 10000 },
    );
  };

  const planFromPlace = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const query = originQuery.trim();
    if (!query) { setRouteStatus("Enter an address or named place inside the study area."); return; }
    setRouteBusy(true);
    setRouteStatus(`Finding “${query}” with OpenStreetMap Nominatim…`);
    planPlaceRoute(runId, query, routeModeRef.current).then((result) => {
      const longitude = Number(result.summary.origin_longitude);
      const latitude = Number(result.summary.origin_latitude);
      if (!Number.isFinite(longitude) || !Number.isFinite(latitude)) {
        throw new Error("The geocoder returned an invalid origin coordinate.");
      }
      showRouteResult(result, longitude, latitude);
    }).catch((reason: unknown) => {
      setRouteStatus(reason instanceof Error ? reason.message : "Could not plan from this location.");
    }).finally(() => setRouteBusy(false));
  };

  const startPanelDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || !routeControlRef.current) return;
    const rect = routeControlRef.current.getBoundingClientRect();
    dragRef.current = {
      pointerId: event.pointerId,
      offsetX: event.clientX - rect.left,
      offsetY: event.clientY - rect.top,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const movePanel = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    const panel = routeControlRef.current;
    const container = containerRef.current;
    if (!drag || drag.pointerId !== event.pointerId || !panel || !container) return;
    routeManuallyMovedRef.current = true;
    const bounds = container.getBoundingClientRect();
    const panelRect = panel.getBoundingClientRect();
    setRoutePosition({
      x: Math.max(6, Math.min(event.clientX - bounds.left - drag.offsetX, bounds.width - panelRect.width - 6)),
      y: Math.max(6, Math.min(event.clientY - bounds.top - drag.offsetY, bounds.height - panelRect.height - 6)),
    });
  };

  const stopPanelDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (dragRef.current?.pointerId === event.pointerId) dragRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };

  const handleLayersToggle = () => {
    window.requestAnimationFrame(() => {
      const layersPanel = layersControlRef.current;
      const routePanel = routeControlRef.current;
      const container = containerRef.current;
      if (routeManuallyMovedRef.current) return;
      if (!layersPanel?.open) {
        if (mobile && routePanel && container) {
          const bounds = container.getBoundingClientRect();
          const routeRect = routePanel.getBoundingClientRect();
          setRoutePosition({
            x: Math.max(6, (bounds.width - routeRect.width) / 2),
            y: DEFAULT_ROUTE_POSITION.y,
          });
        } else {
          setRoutePosition(DEFAULT_ROUTE_POSITION);
        }
        return;
      }
      if (!routePanel || !container) return;
      const layersRect = layersPanel.getBoundingClientRect();
      const routeRect = routePanel.getBoundingClientRect();
      const overlaps = layersRect.left < routeRect.right && layersRect.right > routeRect.left
        && layersRect.top < routeRect.bottom && layersRect.bottom > routeRect.top;
      if (!overlaps) return;
      const bounds = container.getBoundingClientRect();
      setRoutePosition((current) => ({
        ...current,
        y: Math.max(6, Math.min(bounds.height - routeRect.height - 6, current.y + layersRect.bottom - routeRect.top + 8)),
      }));
    });
  };

  if (!url) return <p className="empty-tab">A map is produced after a successful spatial analysis.</p>;
  if (error) return <p className="error error--block">{error}</p>;
  const range = data?.metadata.value_range;
  const displayIndicator = data ? indicatorLabel(data.metadata.indicator_label) : "";
  const routeWarning = !routeBusy && /outside the (?:current study area|analyzed boundary)|not found|denied|could not|failed|timed out|not available|invalid/i.test(routeStatus);
  return <div className="map-view">
    <div ref={containerRef} className="map-canvas" aria-label="Interactive analysis map" />
    {!data && <p className="map-loading">Loading interactive map…</p>}
    {data && <>
      {showRoutePlanner && <div ref={routeControlRef} className={`map-route-control${routeCollapsed ? " is-collapsed" : ""}`} style={{ transform: `translate(${routePosition.x}px, ${routePosition.y}px)` }}>
        <div className="map-route-control__handle" onPointerDown={startPanelDrag} onPointerMove={movePanel} onPointerUp={stopPanelDrag} onPointerCancel={stopPanelDrag}>
          <strong>Point route planner</strong><span aria-hidden="true">Drag</span>
          <button type="button" className="map-control-toggle" aria-expanded={!routeCollapsed} aria-label={routeCollapsed ? "Expand route planner" : "Collapse route planner"} onPointerDown={(event) => event.stopPropagation()} onClick={() => setRouteCollapsed((value) => !value)}>{routeCollapsed ? "+" : "−"}</button>
        </div>
        {!routeCollapsed && <div className="map-route-control__body">
        <p className="map-route-hint">Click a point on the map or enter an address inside the study area.</p>
        <p className="map-route-scope"><b>Route choice:</b> fastest ORS route · <b>Environment:</b> post-route audit · heat-aware routing is not enabled</p>
        <select value={routeMode} onChange={(event) => setRouteMode(event.target.value as RouteMode)} aria-label="Route mode">
          <option value="walking">Walking</option><option value="cycling">Cycling</option><option value="driving">Driving</option>
        </select>
        <div><button type="button" disabled={!mapReady || routeBusy} onClick={toggleMapPlanning}>{planningEnabled ? "Cancel map selection" : "Plan from map point"}</button>
        <button type="button" className="button--secondary" disabled={!mapReady || routeBusy} onClick={useLocation}>Use my location</button></div>
        <form className="map-route-address" onSubmit={planFromPlace}>
          <input value={originQuery} onChange={(event) => setOriginQuery(event.target.value)} placeholder="Address inside study area" aria-label="Route origin address" />
          <button type="submit" className="button--secondary" disabled={!mapReady || routeBusy || !originQuery.trim()}>Plan</button>
        </form>
        <small className={`map-route-status${routeWarning ? " is-warning" : ""}`} role={routeWarning ? "alert" : "status"} aria-live="polite">{routeBusy ? "Planning route…" : routeStatus}</small>
        {routeAudit && <RouteAudit summary={routeAudit} />}
        <details className="map-route-method"><summary>How this route is calculated</summary>
          <p>The origin is your map click, browser coordinate, or a Nominatim-geocoded US address inside the displayed study area. The nearest selected facilities are shortlisted by straight-line distance, then OpenRouteService network travel time chooses the fastest one and supplies its route geometry. This small route request remains available when the full study-area ORS matrix is too large. Route LST, NDVI, NDBI, and albedo are current tract-level, length-weighted overlays, not street-level optimization inputs.</p>
        </details>
        </div>}
      </div>}
      <div className="map-north-arrow" title="Points to geographic north; use the map compass to reset north-up." aria-label="Geographic north indicator"><div className="map-north-arrow__bearing" style={{ transform: `rotate(${-bearing}deg)` }}><span>N</span><i /></div></div>
      <details ref={layersControlRef} className="map-layers" onToggle={handleLayersToggle}><summary>Layers</summary><div>
        {(["tracts", "boundary", "route", "sites"] as const).map((name) => <label key={name}><input type="checkbox" checked={layers[name]} onChange={() => setLayers((current) => ({ ...current, [name]: !current[name] }))} />
          {name === "tracts" ? displayIndicator : name === "sites" ? "Selected candidate facilities" : name === "route" ? "Planned route" : "Study boundary"}</label>)}
      </div></details>
      {paretoPreview && <div className="map-pareto-preview" role="status"><span><strong>Pareto preview</strong> α = {paretoPreview.alpha.toFixed(1)} · gold markers · current result unchanged</span><button type="button" className="button--secondary" onClick={onClearParetoPreview}>Clear</button></div>}
      {range && <div className="map-legend"><strong>{displayIndicator}</strong><span className="map-legend__ramp" style={{ background: `linear-gradient(90deg, ${HEAT_COLORS.join(",")})` }} /><span><small>{range[0].toFixed(1)}</small><small>{range[1].toFixed(1)}</small></span></div>}
    </>}
  </div>;
}
