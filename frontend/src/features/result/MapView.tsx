import { useWorkspacePreferences } from "../../app/WorkspacePreferences";
import { useCallback, useEffect, useRef, useState, type FormEvent, type PointerEvent as ReactPointerEvent } from "react";
import { createPortal } from "react-dom";
import type { Feature, FeatureCollection, Point } from "geojson";
import maplibregl, { type GeoJSONSource, type Map as MapLibreMap } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useBasemapStyle } from "./useBasemapStyle";
import { uploadedCandidateLayer, useUploadedCandidates } from "./useUploadedCandidates";
import { LoadingText } from "../../components/LoadingText";
import { planPlaceRoute, planPointRoute, type PlannedRoute } from "../../api/client";
import { HEAT_COLORS, MAP_LAYER_IDS, MAP_SOURCE_ID, PARETO_SOURCE_ID, ROUTE_SOURCE_ID, paretoPreviewLayer, plannedRouteLayers, resultLayers } from "./mapConfig";
import { popupContent } from "./mapPopup";
import type { ParetoPoint } from "./ParetoView";
import { useAppStore } from "../../store/appStore";

type HeatMetric = "landsat_lst" | "utci";
interface ThermalLayer {
  label: string; unit: string; source: string; source_url: string;
  start_date: string; end_date: string; resolution: string; aggregation: string;
  field: string; value_range: [number, number]; source_cells?: number;
}
interface MapMetadata {
  renderer: "maplibre";
  indicator: string;
  indicator_label: string;
  value_range: [number, number] | null;
  bounds: [[number, number], [number, number]];
  thermal_layers?: Partial<Record<HeatMetric, ThermalLayer>>;
  run_heat_metric?: HeatMetric;
  siting_heat_weight?: number | null;
  candidate_source?: "osm" | "csv";
}
interface MapDataset extends FeatureCollection { metadata: MapMetadata; }
interface MapViewProps { initialRouteMode?: RouteMode; url?: string; runId: string; paretoPreview?: ParetoPoint | null; onClearParetoPreview?: () => void; showRoutePlanner?: boolean; initialRouteCollapsed?: boolean; focusTractId?: string | null; siteSlot?: HTMLDivElement | null; routeOriginQuery?: string | null; routeOriginCoordinate?: { longitude: number; latitude: number } | null; }
type RouteMode = "walking" | "cycling" | "driving";
type PanelPosition = { x: number; y: number };

const EMPTY_ROUTE: FeatureCollection = { type: "FeatureCollection", features: [] };
const DEFAULT_ROUTE_POSITION: PanelPosition = { x: 10, y: 52 };

function indicatorLabel(label: string): string {
  return /^Heat exposure\s*\(/i.test(label) ? "Heat exposure (°C)" : label;
}

function siteKey(feature: Pick<Feature, "geometry" | "properties">) {
  return `site:${feature.properties?.candidate_id ?? (feature.geometry.type === "Point" ? feature.geometry.coordinates.join(",") : "")}`;
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

export function MapView({ initialRouteMode = "walking", url, runId, paretoPreview, onClearParetoPreview, showRoutePlanner = true, initialRouteCollapsed, focusTractId = null, siteSlot, routeOriginQuery = null, routeOriginCoordinate = null }: MapViewProps) {
  const { t } = useWorkspacePreferences();
  const baseMapStyle = useBasemapStyle();
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const routeControlRef = useRef<HTMLDivElement>(null);
  const layersControlRef = useRef<HTMLDetailsElement>(null);
  const dragRef = useRef<{ pointerId: number; offsetX: number; offsetY: number } | null>(null);
  const routeManuallyMovedRef = useRef(false);
  const planAtRef = useRef<((longitude: number, latitude: number) => void) | null>(null);
  const selectSiteRef = useRef<((site: Feature<Point>) => void) | null>(null);
  const routeRequestRef = useRef<AbortController | null>(null);
  const autoRouteRef = useRef<string | null>(null);
  const outsideReturnTimerRef = useRef<number | null>(null);
  const planningEnabledRef = useRef(false);
  const routeModeRef = useRef<RouteMode>(initialRouteMode);
  const [data, setData] = useState<MapDataset | null>(null);
  const [heatMetric, setHeatMetric] = useState<HeatMetric | null>(null);
  const thermalLayers = data?.metadata.thermal_layers;
  const runHeatMetric = data?.metadata.run_heat_metric ?? "landsat_lst";
  const visibleHeat = (heatMetric && thermalLayers?.[heatMetric] && heatMetric)
    || (thermalLayers?.[runHeatMetric] && runHeatMetric) || (thermalLayers?.utci ? "utci" : "landsat_lst");
  const thermalLayer = thermalLayers?.[visibleHeat];
  const sitingHeatWeight = data?.metadata.siting_heat_weight;
  const isRunHeat = visibleHeat === runHeatMetric;
  const heatRole = !isRunHeat ? "View only"
    : sitingHeatWeight == null ? "Run heat input"
      : sitingHeatWeight > 0 ? "Used in siting weights" : "Zero siting heat weight";
  const range = thermalLayer?.value_range ?? data?.metadata.value_range;
  const [error, setError] = useState<string | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [selectedSite, setSelectedSite] = useState<string | null>(null);
  const [sitesOpen, setSitesOpen] = useState(false);
  const [mapReady, setMapReady] = useState(false);
  const [bearing, setBearing] = useState(0);
  const [planningEnabled, setPlanningEnabled] = useState(false);
  const [routeMode, setRouteMode] = useState<RouteMode>(initialRouteMode);
  const [routeStatus, setRouteStatus] = useState('Select “Plan from map point”, then click an exact origin.');
  const [routeBusy, setRouteBusy] = useState(false);
  const [routeAudit, setRouteAudit] = useState<Record<string, unknown> | null>(null);
  const [originQuery, setOriginQuery] = useState("");
  const [routeCollapsed, setRouteCollapsed] = useState(initialRouteCollapsed ?? false);
  const [routePosition, setRoutePosition] = useState<PanelPosition>(DEFAULT_ROUTE_POSITION);
  const [layers, setLayers] = useState({ route: true, sites: true });
  const [layersOpen, setLayersOpen] = useState(true);
  const visibility = useAppStore((state) => state.studyLayerVisibility);
  const setVisible = useAppStore((state) => state.setStudyLayerVisibility);
  const hasCandidates = Boolean(data?.features.some((feature) => feature.properties?.layer_kind === "candidate"));
  const candidateKey = data?.metadata.candidate_source === "csv" ? "uploaded" : "candidate";
  const uploaded = useUploadedCandidates(mapRef, mapReady, !planningEnabled, Boolean(siteSlot) && !(candidateKey === "uploaded" && hasCandidates));

  useEffect(() => { planningEnabledRef.current = planningEnabled; if (mapRef.current) mapRef.current.getCanvas().style.cursor = planningEnabled ? "crosshair" : ""; }, [planningEnabled]);
  useEffect(() => { routeModeRef.current = routeMode; }, [routeMode]);
  useEffect(() => {
    const clampPanel = () => {
      const panel = routeControlRef.current;
      const container = containerRef.current;
      const layersPanel = layersControlRef.current;
      if (!panel || !container || !layersPanel) return;
      const bounds = container.getBoundingClientRect();
      const panelRect = panel.getBoundingClientRect();
      const layerRect = layersPanel.getBoundingClientRect();
      const beside = layerRect.right - bounds.left + 8;
      const below = layerRect.bottom - bounds.top + 8;
      setRoutePosition((current) => {
        const maxX = Math.max(6, bounds.width - panelRect.width - 6);
        const next = {
          x: Math.max(6, Math.min(current.x, maxX)),
          y: Math.max(6, Math.min(current.y, bounds.height - 52)),
        };
        const overlaps = next.x < beside && next.x + panelRect.width > layerRect.left - bounds.left
          && next.y < below && next.y + panelRect.height > layerRect.top - bounds.top;
        if (!routeManuallyMovedRef.current || overlaps) {
          next.x = DEFAULT_ROUTE_POSITION.x;
          next.y = below;
        }
        return current.x === next.x && current.y === next.y ? current : next;
      });
    };
    clampPanel();
    const observer = new ResizeObserver(clampPanel);
    if (routeControlRef.current) observer.observe(routeControlRef.current);
    if (layersControlRef.current) observer.observe(layersControlRef.current);
    if (containerRef.current) observer.observe(containerRef.current);
    window.addEventListener("resize", clampPanel);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", clampPanel);
    };
  }, [mapReady, routeCollapsed, layersOpen]);

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

  const cancelRoute = useCallback(() => {
    routeRequestRef.current?.abort();
    routeRequestRef.current = null;
    setRouteBusy(false);
    setPlanningEnabled(false);
    planningEnabledRef.current = false;
    setRouteStatus("Route request cancelled. Choose another point or retry.");
  }, []);

  useEffect(() => useAppStore.subscribe((state, previous) => {
    if (state.taskVersion !== previous.taskVersion && (routeRequestRef.current || planningEnabledRef.current)) cancelRoute();
  }), [cancelRoute]);

  const requestRoute = useCallback((request: (signal: AbortSignal) => Promise<PlannedRoute>, coordinate?: { longitude: number; latitude: number }) => {
    routeRequestRef.current?.abort();
    const controller = new AbortController();
    routeRequestRef.current = controller;
    setRouteBusy(true);
    setPlanningEnabled(false);
    planningEnabledRef.current = false;
    void request(controller.signal).then((result) => {
      if (routeRequestRef.current !== controller) return;
      const longitude = coordinate?.longitude ?? Number(result.summary.origin_longitude);
      const latitude = coordinate?.latitude ?? Number(result.summary.origin_latitude);
      if (!Number.isFinite(longitude) || !Number.isFinite(latitude)) throw new Error("The geocoder returned an invalid origin coordinate.");
      showRouteResult(result, longitude, latitude);
    }).catch((reason: unknown) => {
      if (routeRequestRef.current !== controller) return;
      setRouteStatus(reason instanceof Error && reason.name === "TimeoutError"
        ? "Route request timed out. Your inputs are kept; retry or choose another point."
        : reason instanceof Error ? reason.message : "Could not plan this route.");
    }).finally(() => {
      if (routeRequestRef.current === controller) { routeRequestRef.current = null; setRouteBusy(false); }
    });
  }, [showRouteResult]);

  useEffect(() => {
    return () => { routeRequestRef.current?.abort(); routeRequestRef.current = null; };
  }, [runId, url]);

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
    requestRoute((signal) => coordinate
      ? planPointRoute(runId, coordinate.longitude, coordinate.latitude, routeModeRef.current, signal)
      : planPlaceRoute(runId, query as string, routeModeRef.current, signal), coordinate ?? undefined);
  }, [mapReady, routeOriginCoordinate, routeOriginQuery, runId, showRoutePlanner, requestRoute, url]);

  useEffect(() => {
    if (!url) { setData(null); return; }
    const controller = new AbortController();
    setError(null);
    setData(null);
    setMapReady(false);
    fetch(url, { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15_000)]) }).then((response) => {
      if (!response.ok) throw new Error(`Map data request failed (${response.status})`);
      return response.json() as Promise<MapDataset>;
    }).then((payload) => {
      if (payload.type !== "FeatureCollection" || payload.metadata?.renderer !== "maplibre") throw new Error("The map artifact is not valid OASIS MapLibre data.");
      if (!controller.signal.aborted) setData(payload);
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) setError(reason instanceof Error && reason.name === "TimeoutError"
        ? "Map data request timed out. Retry loading the map."
        : reason instanceof Error ? reason.message : "Could not load map data.");
    });
    return () => controller.abort();
  }, [url, loadAttempt]);

  useEffect(() => {
    if (!data || !containerRef.current) return;
    setMapReady(false);
    let map: MapLibreMap;
    try {
      map = new maplibregl.Map({ container: containerRef.current, style: baseMapStyle, attributionControl: false });
    } catch {
      setError("The interactive map could not start. Check browser WebGL support and retry loading the map.");
      return;
    }
    mapRef.current = map;
    const mapSizeObserver = new ResizeObserver(() => map.resize());
    mapSizeObserver.observe(containerRef.current);
    map.addControl(new maplibregl.NavigationControl({ showCompass: true, visualizePitch: true }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");
    map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");
    const updateBearing = () => setBearing(map.getBearing());
    map.on("rotate", updateBearing);
    let popup: maplibregl.Popup | null = null;

    // Result layers must not wait for remote basemap tiles to finish loading.
    map.once("style.load", () => {
      map.addSource(MAP_SOURCE_ID, { type: "geojson", data });
      map.addSource(ROUTE_SOURCE_ID, { type: "geojson", data: EMPTY_ROUTE });
      map.addSource(PARETO_SOURCE_ID, { type: "geojson", data: EMPTY_ROUTE });
      resultLayers(data.metadata.value_range).forEach((layer) => map.addLayer(layer, layer.id === MAP_LAYER_IDS.demand ? MAP_LAYER_IDS.tracts : undefined));
      plannedRouteLayers().forEach((layer) => map.addLayer(layer));
      map.addLayer(paretoPreviewLayer);
      map.fitBounds(data.metadata.bounds, { padding: 30, duration: 0, maxZoom: 13 });
      setMapReady(true);

      planAtRef.current = (longitude, latitude) => {
        const origin: Feature<Point> = { type: "Feature", geometry: { type: "Point", coordinates: [longitude, latitude] }, properties: { layer_kind: "route_origin" } };
        (map.getSource(ROUTE_SOURCE_ID) as GeoJSONSource).setData({ type: "FeatureCollection", features: [origin] });
        setRouteBusy(true);
        setRouteStatus("Origin selected. Comparing network travel times to selected candidate facilities…");
        requestRoute((signal) => planPointRoute(runId, longitude, latitude, routeModeRef.current, signal), { longitude, latitude });
      };

      let pinnedFeature: string | null = null;
      const showPopup = (event: Pick<maplibregl.MapMouseEvent, "lngLat"> & { features?: Pick<Feature, "geometry" | "properties">[] }, closeButton = false, key: string | null = null) => {
        const feature = event.features?.[0]; if (!feature) return;
        popup?.remove();
        pinnedFeature = key;
        if (siteSlot && key?.startsWith("site:")) {
          setSelectedSite(key);
          setSitesOpen(true);
        }
        if (closeButton) map.easeTo({ center: event.lngLat, duration: 350 });
        const point = feature.geometry.type === "Point" ? feature.geometry.coordinates : [event.lngLat.lng, event.lngLat.lat];
        const coordinates: [number, number] = [Number(point[0]), Number(point[1])];
        const nextPopup = new maplibregl.Popup({ closeButton, closeOnClick: false, offset: 10, className: closeButton ? "" : "map-hover-popup" }).setLngLat(event.lngLat).setDOMContent(popupContent(feature.properties ?? {}, coordinates, closeButton)).addTo(map);
        popup = nextPopup;
        nextPopup.on("close", () => {
          if (popup === nextPopup) { popup = null; pinnedFeature = null; setSelectedSite(null); }
        });
      };
      selectSiteRef.current = (feature) => {
        const [longitude, latitude] = feature.geometry.coordinates;
        if (pinnedFeature === siteKey(feature)) popup?.remove();
        else showPopup({ lngLat: new maplibregl.LngLat(longitude, latitude), features: [feature] }, true, siteKey(feature));
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
              { lngLat: center, features: [target] },
              true, `tract:${focusTractId}`,
            );
          }
        }
      }
      const interactiveLayers = [MAP_LAYER_IDS.tracts, MAP_LAYER_IDS.demand, MAP_LAYER_IDS.boundary, MAP_LAYER_IDS.candidates, MAP_LAYER_IDS.sites, MAP_LAYER_IDS.paretoSites, MAP_LAYER_IDS.route];
      map.on("mousemove", (event) => {
        if (pinnedFeature || planningEnabledRef.current) return;
        const features = map.queryRenderedFeatures(event.point, { layers: interactiveLayers });
        const onUpload = map.getLayer(uploadedCandidateLayer)
          && map.queryRenderedFeatures(event.point, { layers: [uploadedCandidateLayer] }).length;
        const feature = onUpload ? features.find((item) => item.layer.id === MAP_LAYER_IDS.sites) : features[0];
        if (feature) showPopup({ ...event, features: [feature] });
        else popup?.remove();
      });
      map.getCanvas().addEventListener("mouseleave", () => { if (!pinnedFeature) popup?.remove(); });
      map.on("click", (event) => {
        if (planningEnabledRef.current) {
          popup?.remove(); pinnedFeature = null;
          planAtRef.current?.(event.lngLat.lng, event.lngLat.lat);
          return;
        }
        const features = map.queryRenderedFeatures(event.point, { layers: interactiveLayers });
        const site = features.find((item) => item.layer.id === MAP_LAYER_IDS.sites);
        if (!site && map.getLayer(uploadedCandidateLayer) && map.queryRenderedFeatures(event.point, { layers: [uploadedCandidateLayer] }).length) {
          popup?.remove();
          return;
        }
        if (pinnedFeature && (!siteSlot || !site || siteKey(site) === pinnedFeature)) {
          pinnedFeature = null; popup?.remove(); popup = null;
          return;
        }
        const feature = site
          ?? features.find((item) => item.layer.id === MAP_LAYER_IDS.paretoSites)
          ?? features[0];
        if (!feature) return;
        const properties = feature.properties ?? {};
        const prefix = feature.layer.id === MAP_LAYER_IDS.sites ? "site" : String(properties.layer_kind ?? feature.layer.id);
        const id = properties.GEOID ?? properties.destination_id ?? properties.candidate_id ?? feature.id;
        showPopup({ ...event, features: [feature] }, true, prefix === "site" ? siteKey(feature) : `${prefix}:${String(id ?? "unknown")}`);
      });
      interactiveLayers.forEach((id) => {
        map.on("mouseenter", id, () => { if (!planningEnabledRef.current) map.getCanvas().style.cursor = "pointer"; });
        map.on("mouseleave", id, () => { map.getCanvas().style.cursor = planningEnabledRef.current ? "crosshair" : ""; });
      });
    });
    return () => {
      if (outsideReturnTimerRef.current !== null) {
        window.clearTimeout(outsideReturnTimerRef.current);
        outsideReturnTimerRef.current = null;
      }
      mapSizeObserver.disconnect(); planAtRef.current = null; selectSiteRef.current = null; popup?.remove();
      map.off("rotate", updateBearing); map.remove(); mapRef.current = null;
      setMapReady(false); setBearing(0);
    };
  }, [data, focusTractId, runId, requestRoute, baseMapStyle, siteSlot]);

  useEffect(() => {
    const source = mapRef.current?.getSource(PARETO_SOURCE_ID) as GeoJSONSource | undefined;
    if (!source) return;
    source.setData({ type: "FeatureCollection", features: (paretoPreview?.selected_sites ?? []).map((site) => ({
      type: "Feature", geometry: { type: "Point", coordinates: [site.longitude, site.latitude] },
      properties: { layer_kind: "pareto_preview_site", name: site.name, candidate_id: site.id, alpha: paretoPreview?.alpha },
    })) });
  }, [paretoPreview, mapReady]);

  useEffect(() => {
    const map = mapRef.current; if (!mapReady || !map) return;
    const show = (id: string, visible: boolean) => map.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
    show(MAP_LAYER_IDS.tracts, visibility.heat);
    show(MAP_LAYER_IDS.demand, visibility.demand);
    show(MAP_LAYER_IDS.tractOutline, visibility.demand);
    show(MAP_LAYER_IDS.boundary, visibility.boundary);
    show(MAP_LAYER_IDS.candidates, visibility[candidateKey]);
    show(MAP_LAYER_IDS.sites, layers.sites);
    [MAP_LAYER_IDS.route, MAP_LAYER_IDS.routeOrigin].forEach((id) => show(id, layers.route));
  }, [candidateKey, layers, mapReady, visibility]);

  useEffect(() => {
    const map = mapRef.current;
    if (!mapReady || !map || !range) return;
    const [tracts] = resultLayers(range, thermalLayer?.field);
    map.setPaintProperty(MAP_LAYER_IDS.tracts, "fill-color", tracts.paint?.["fill-color"]);
    map.setPaintProperty(MAP_LAYER_IDS.tracts, "fill-opacity", tracts.paint?.["fill-opacity"]);
  }, [mapReady, range, thermalLayer]);

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
    routeRequestRef.current?.abort();
    const locationRequest = new AbortController();
    routeRequestRef.current = locationRequest;
    setRouteStatus("Waiting for browser location permission…"); setRouteBusy(true);
    navigator.geolocation.getCurrentPosition(
      ({ coords }) => {
        if (routeRequestRef.current !== locationRequest) return;
        setRouteBusy(false);
        setRouteStatus(`Location received (${coords.latitude.toFixed(5)}, ${coords.longitude.toFixed(5)}). Checking whether it is inside the study area…`);
        planAtRef.current?.(coords.longitude, coords.latitude);
      },
      (reason) => {
        if (routeRequestRef.current !== locationRequest) return;
        routeRequestRef.current = null;
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
    requestRoute((signal) => planPlaceRoute(runId, query, routeModeRef.current, signal));
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
    const next = {
      x: Math.max(6, Math.min(event.clientX - bounds.left - drag.offsetX, bounds.width - panelRect.width - 6)),
      y: Math.max(6, Math.min(event.clientY - bounds.top - drag.offsetY, bounds.height - 52)),
    };
    const layerRect = layersControlRef.current?.getBoundingClientRect();
    if (layerRect && next.x < layerRect.right - bounds.left + 8 && next.x + panelRect.width > layerRect.left - bounds.left
      && next.y < layerRect.bottom - bounds.top + 8 && next.y + panelRect.height > layerRect.top - bounds.top) return;
    setRoutePosition(next);
  };

  const stopPanelDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (dragRef.current?.pointerId === event.pointerId) dragRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };

  if (!url) return <p className="empty-tab">A map is produced after a successful spatial analysis.</p>;
  if (error) return <div className="error error--block" role="alert"><p>{error}</p><button type="button" className="button--secondary" onClick={() => setLoadAttempt((attempt) => attempt + 1)}>Retry map</button></div>;
  const sites = siteSlot ? data?.features.filter((feature): feature is Feature<Point> => feature.geometry.type === "Point" && feature.properties?.layer_kind === "selected_site") : undefined;
  const displayIndicator = thermalLayer ? `${thermalLayer.label} (${thermalLayer.unit})` : data ? indicatorLabel(data.metadata.indicator_label) : "";
  const routeWarning = !routeBusy && /outside the (?:current study area|analyzed boundary)|not found|denied|could not|failed|timed out|not available|invalid/i.test(routeStatus);
  return <>
    {siteSlot && thermalLayer && thermalLayers && <section className="map-thermal-bar" aria-label="Study-area heat layer">
      <label><span>{t("Heat layer")}</span>
        <select aria-label={t("Heat layer")} value={visibleHeat} onChange={(event) => setHeatMetric(event.target.value as HeatMetric)}>
          <option value="landsat_lst" disabled={!thermalLayers.landsat_lst}>{t("Landsat LST (°C)")}</option>
          <option value="utci" disabled={!thermalLayers.utci}>{t("ERA5-HEAT UTCI (°C)")}</option>
        </select>
      </label>
      <span role="status" className={`status ${isRunHeat && sitingHeatWeight !== 0 ? "status--success" : ""}`}>{t(heatRole)}</span>
      <details>
        <summary>{t("Source & dates")}</summary>
        <p><a href={thermalLayer.source_url} target="_blank" rel="noreferrer">{thermalLayer.source}</a></p>
        <p>{thermalLayer.start_date} to {thermalLayer.end_date} · {thermalLayer.resolution}</p>
        <p>{thermalLayer.aggregation}</p>
        {thermalLayer.source_cells != null && <p>Native grid cells represented: {thermalLayer.source_cells}</p>}
        {thermalLayer.value_range[0] === thermalLayer.value_range[1] && <p>This layer has the same value in every displayed tract.</p>}
        {isRunHeat && sitingHeatWeight != null && <p>Siting heat coefficient {(sitingHeatWeight * 100).toFixed(1)}%.</p>}
        {!isRunHeat && <p>{t("Changing this layer keeps the current sites.")}</p>}
      </details>
    </section>}
    <div className="map-view">
    {siteSlot && Boolean(sites?.length) && createPortal(<details className="workbench-disclosure workbench-sites" open={sitesOpen} onToggle={(event) => setSitesOpen(event.currentTarget.open)}>
      <summary>Selected sites ({sites?.length})</summary>
      <ul>{sites?.map((site, index) => <li key={siteKey(site)}><button type="button" aria-pressed={selectedSite === siteKey(site)} disabled={!mapReady} onClick={() => {
        if (!layers.sites) setLayers((current) => ({ ...current, sites: true }));
        selectSiteRef.current?.(site);
      }}>{String(site.properties?.name || `Site ${index + 1}`)}</button></li>)}</ul>
    </details>, siteSlot)}
    <div ref={containerRef} className="map-canvas" aria-label="Interactive analysis map" />
    {!mapReady && <p className="map-loading" role="status"><LoadingText>Loading interactive map</LoadingText></p>}
    {data && <>
      {showRoutePlanner && <div ref={routeControlRef} className={`map-route-control${routeCollapsed ? " is-collapsed" : ""}`} style={{ transform: `translate(${routePosition.x}px, ${routePosition.y}px)`, maxHeight: `calc(100% - ${routePosition.y + 6}px)` }}>
        <div className="map-route-control__handle" onPointerDown={startPanelDrag} onPointerMove={movePanel} onPointerUp={stopPanelDrag} onPointerCancel={stopPanelDrag}>
          <strong>{t("Point route planner")}</strong><span aria-hidden="true">Drag</span>
          <button type="button" className="map-control-toggle" aria-expanded={!routeCollapsed} aria-label={routeCollapsed ? "Expand route planner" : "Collapse route planner"} onPointerDown={(event) => event.stopPropagation()} onClick={() => setRouteCollapsed((value) => !value)}>{routeCollapsed ? "+" : "−"}</button>
        </div>
        {!routeCollapsed && <div className="map-route-control__body">
        <p className="map-route-hint">Click a point on the map or enter an address inside the study area.</p>
        <p className="map-route-scope"><b>Route choice:</b> fastest ORS route · <b>Environment:</b> post-route audit · heat-aware routing is not enabled</p>
        <select value={routeMode} onChange={(event) => setRouteMode(event.target.value as RouteMode)} aria-label="Route mode">
          <option value="walking">{t("Walking")}</option><option value="cycling">{t("Cycling")}</option><option value="driving">{t("Driving")}</option>
        </select>
        <div><button type="button" disabled={!mapReady || routeBusy} onClick={toggleMapPlanning}>{planningEnabled ? "Cancel map selection" : "Plan from map point"}</button>
        <button type="button" className="button--secondary" disabled={!mapReady || routeBusy} onClick={useLocation}>{t("Use my location")}</button></div>
        <form className="map-route-address" onSubmit={planFromPlace}>
          <input value={originQuery} onChange={(event) => setOriginQuery(event.target.value)} placeholder="Address inside study area" aria-label="Route origin address" />
          <button type="submit" className="button--secondary" disabled={!mapReady || routeBusy || !originQuery.trim()}>Plan</button>
        </form>
        {routeBusy && <button type="button" onClick={cancelRoute}>Cancel route request</button>}
        <small className={`map-route-status${routeWarning ? " is-warning" : ""}`} role={routeWarning ? "alert" : "status"} aria-live="polite"><LoadingText active={routeBusy}>{routeBusy ? "Planning route" : routeStatus}</LoadingText></small>
        {routeAudit && <RouteAudit summary={routeAudit} />}
        <details className="map-route-method"><summary>How this route is calculated</summary>
          <p>The route starts at your map point, browser location, or an address inside the study area. OpenRouteService returns the fastest network route from a shortlist of nearby selected sites. The heat audit uses length-weighted demand-zone overlays.</p>
        </details>
        </div>}
      </div>}
      <div className="map-north-arrow" title="Points to geographic north; use the map compass to reset north-up." aria-label="Geographic north indicator"><div className="map-north-arrow__bearing" style={{ transform: `rotate(${-bearing}deg)` }}><span>N</span><i /></div></div>
      <details ref={layersControlRef} className="map-layers" open={layersOpen} onToggle={(event) => setLayersOpen(event.currentTarget.open)}><summary>{t("Layers")}</summary><div>
        <label><input type="checkbox" checked={visibility.boundary} onChange={(event) => setVisible("boundary", event.target.checked)} />{t("Study boundary")}</label>
        <label><input type="checkbox" checked={visibility.demand} onChange={(event) => setVisible("demand", event.target.checked)} />{t("Demand zones")}</label>
        {range && <label><input type="checkbox" checked={visibility.heat} onChange={(event) => setVisible("heat", event.target.checked)} />{data.metadata.indicator === "heat_exposure" ? t("Heat layer") : displayIndicator}</label>}
        {hasCandidates && <label><input type="checkbox" checked={visibility[candidateKey]} onChange={(event) => setVisible(candidateKey, event.target.checked)} />{t(candidateKey === "uploaded" ? "Uploaded candidates" : "Candidate facilities")}</label>}
        <label><input type="checkbox" checked={layers.sites} onChange={(event) => setLayers((current) => ({ ...current, sites: event.target.checked }))} />{t("Selected candidate facilities")}</label>
        {showRoutePlanner && <label><input type="checkbox" checked={layers.route} onChange={(event) => setLayers((current) => ({ ...current, route: event.target.checked }))} />{t("Planned route")}</label>}
        {siteSlot && uploaded.count > 0 && <>
          <label><input type="checkbox" checked={uploaded.visible} onChange={(event) => uploaded.setVisible(event.target.checked)} />{t("Uploaded candidates")} ({uploaded.count})</label>
          <button type="button" className="button--secondary" onClick={uploaded.zoom}>{t("Zoom to uploaded points")}</button>
        </>}
      </div></details>
      {paretoPreview && <div className="map-pareto-preview" role="status"><span><strong>Pareto preview</strong> α = {paretoPreview.alpha.toFixed(1)} · gold markers · current result unchanged</span><button type="button" className="button--secondary" onClick={onClearParetoPreview}>Clear</button></div>}
      {range && visibility.heat && <div className="map-legend"><strong>{displayIndicator}</strong><span className="map-legend__ramp" style={{ background: `linear-gradient(90deg, ${HEAT_COLORS.join(",")})` }} /><span><small>{range[0].toFixed(1)}</small><small>{range[1].toFixed(1)}</small></span></div>}
    </>}
    </div>
  </>;
}
