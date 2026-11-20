import { useEffect, useMemo, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import type { FeatureCollection, Polygon, MultiPolygon } from "geojson";
import "maplibre-gl/dist/maplibre-gl.css";
import { useBasemapStyle } from "../features/result/useBasemapStyle";
import { uploadedCandidateLayer, useUploadedCandidates } from "../features/result/useUploadedCandidates";
import { MAP_LAYER_IDS, MAP_SOURCE_ID, resultLayers } from "../features/result/mapConfig";
import { popupContent } from "../features/result/mapPopup";
import { useAppStore } from "../store/appStore";
import type { StudyInputLayers } from "../api/models";
import { useWorkspacePreferences } from "./WorkspacePreferences";

const EMPTY: FeatureCollection = { type: "FeatureCollection", features: [] };

export function StudyMap({ inputLayers, uploadedRun, place }: {
  inputLayers: StudyInputLayers; uploadedRun?: boolean; place?: string | null;
}) {
  const style = useBasemapStyle();
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const [ready, setReady] = useState(false);
  const lastFit = useRef<string | null>(null);
  const [layersOpen, setLayersOpen] = useState(true);
  const [heatField, setHeatField] = useState<"utci_c" | "lst_exposure_c">("utci_c");
  const visibility = useAppStore((state) => state.studyLayerVisibility);
  const setVisible = useAppStore((state) => state.setStudyLayerVisibility);
  const { t } = useWorkspacePreferences();
  const { boundary, tract, candidate } = inputLayers;
  const uploaded = useUploadedCandidates(mapRef, ready, true, !(uploadedRun && candidate?.features.length));
  const [failedStyle, setFailedStyle] = useState<typeof style>();
  const canDisplay = Boolean(boundary?.features.length || tract?.features.length || candidate?.features.length) || uploaded.count > 0
    || (typeof style === "object" && style !== null && Boolean(style.sources.carto));
  const data = useMemo<FeatureCollection>(() => ({
    type: "FeatureCollection",
    features: ([["boundary", boundary], ["tract", tract], ["candidate", candidate]] as const)
      .flatMap(([kind, collection]) => (collection?.features ?? []).map((feature) => ({
        ...feature, properties: { ...feature.properties, layer_kind: kind },
      }))),
  }), [boundary, tract, candidate]);
  const thermalLayers = useMemo(() => {
    return (["utci_c", "lst_exposure_c"] as const).flatMap((field) => {
      let min = Infinity;
      let max = -Infinity;
      for (const { properties } of tract?.features ?? []) {
        const value = properties?.[field];
        if (typeof value !== "number" || !Number.isFinite(value)) continue;
        min = Math.min(min, value);
        max = Math.max(max, value);
      }
      return Number.isFinite(min) ? [{ field, range: [min, max] as [number, number] }] : [];
    });
  }, [tract]);
  const heat = thermalLayers.find(({ field }) => field === heatField) ?? thermalLayers[0];

  useEffect(() => {
    if (!container.current || !canDisplay) return;
    let map: maplibregl.Map;
    try {
      map = new maplibregl.Map({
        container: container.current, style, center: [0, 20], zoom: 1.2, attributionControl: false,
      });
    } catch {
      setFailedStyle(style);
      return;
    }
    mapRef.current = map;
    lastFit.current = null;
    let popup: maplibregl.Popup | undefined;
    map.once("style.load", () => {
      map.addSource(MAP_SOURCE_ID, { type: "geojson", data: EMPTY });
      for (const layer of resultLayers(null)) {
        if (layer.id !== MAP_LAYER_IDS.sites) map.addLayer(layer, layer.id === MAP_LAYER_IDS.demand ? MAP_LAYER_IDS.tracts : undefined);
      }
      setReady(true);
      map.on("mousemove", (event) => {
        const onUpload = map.getLayer(uploadedCandidateLayer)
          && map.queryRenderedFeatures(event.point, { layers: [uploadedCandidateLayer] }).length;
        const properties = onUpload ? undefined : map.queryRenderedFeatures(event.point, {
          layers: [MAP_LAYER_IDS.candidates, MAP_LAYER_IDS.tractOutline, MAP_LAYER_IDS.demand, MAP_LAYER_IDS.tracts, MAP_LAYER_IDS.boundary],
        })[0]?.properties;
        if (!properties) {
          popup?.remove();
          return;
        }
        popup ??= new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 8, className: "map-hover-popup" });
        popup.setLngLat(event.lngLat).setDOMContent(popupContent(properties, undefined, false));
        if (!popup.isOpen()) popup.addTo(map);
      });
      map.getCanvas().addEventListener("mouseleave", () => popup?.remove());
    });
    map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");
    map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");
    const observer = new ResizeObserver(() => map.resize());
    observer.observe(container.current);
    return () => { observer.disconnect(); popup?.remove(); mapRef.current = null; map.remove(); setReady(false); };
  }, [canDisplay, style]);

  const hasBoundary = Boolean(boundary?.features.length);
  const hasTracts = Boolean(tract?.features.length);
  const zoomToUpload = uploaded.zoom;
  useEffect(() => { if (!hasBoundary && !hasTracts) zoomToUpload(); }, [hasBoundary, hasTracts, zoomToUpload]);
  useEffect(() => {
    const map = mapRef.current;
    if (ready) map?.getSource<maplibregl.GeoJSONSource>(MAP_SOURCE_ID)?.setData(data);
  }, [data, ready]);
  useEffect(() => {
    const map = mapRef.current;
    const areas = hasBoundary ? boundary : tract;
    const fitKey = `${place ?? ""}:${hasBoundary ? "boundary" : "demand"}`;
    if (!ready || !map || !areas?.features.length || lastFit.current === fitKey) return;
    const bounds = new maplibregl.LngLatBounds();
    for (const { geometry } of areas.features) {
      if (!geometry || !["Polygon", "MultiPolygon"].includes(geometry.type)) continue;
      const polygon = geometry as Polygon | MultiPolygon;
      const rings = polygon.type === "Polygon" ? polygon.coordinates : polygon.coordinates.flat();
      for (const ring of rings) for (const [longitude, latitude] of ring) bounds.extend([longitude, latitude]);
    }
    if (!bounds.isEmpty()) {
      lastFit.current = fitKey;
      map.fitBounds(bounds, { padding: 58, maxZoom: 15, duration: 350 });
    }
  }, [boundary, hasBoundary, place, ready, tract]);
  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map?.getLayer(MAP_LAYER_IDS.tracts)) return;
    if (heat) {
      const [layer] = resultLayers(heat.range, heat.field);
      map.setPaintProperty(MAP_LAYER_IDS.tracts, "fill-color", layer.paint?.["fill-color"]);
      map.setPaintProperty(MAP_LAYER_IDS.tracts, "fill-opacity", layer.paint?.["fill-opacity"]);
    }
    const show = (id: string, visible: boolean) => map.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
    show(MAP_LAYER_IDS.tracts, Boolean(heat) && visibility.heat);
    show(MAP_LAYER_IDS.demand, visibility.demand);
    show(MAP_LAYER_IDS.tractOutline, visibility.demand);
    show(MAP_LAYER_IDS.boundary, visibility.boundary);
    show(MAP_LAYER_IDS.candidates, uploadedRun ? visibility.uploaded : visibility.candidate);
  }, [heat, ready, uploadedRun, visibility]);

  return <>
    <div ref={container} className="study-map__basemap" aria-label="Study area overview map" hidden={!canDisplay || failedStyle === style} />
    {(data.features.length > 0 || uploaded.count > 0) && <details className="map-layers study-map__preview-control" open={layersOpen} onToggle={(event) => setLayersOpen(event.currentTarget.open)}>
      <summary>{t("Layers")}</summary>
      <div>
        {hasBoundary && <label><input type="checkbox" checked={visibility.boundary} onChange={(event) => setVisible("boundary", event.target.checked)} />{t("Study boundary")}</label>}
        {hasTracts && <label><input type="checkbox" checked={visibility.demand} onChange={(event) => setVisible("demand", event.target.checked)} />{t("Demand zones")} ({tract?.features.length})</label>}
        {heat && <label><input type="checkbox" checked={visibility.heat} onChange={(event) => setVisible("heat", event.target.checked)} />{t("Heat layer")}</label>}
        {heat && thermalLayers.length > 1 && <select aria-label={t("Heat layer")} value={heat.field} onChange={(event) => setHeatField(event.target.value === "utci_c" ? "utci_c" : "lst_exposure_c")}>
          <option value="utci_c">{t("ERA5-HEAT UTCI (°C)")}</option>
          <option value="lst_exposure_c">{t("Landsat LST (°C)")}</option>
        </select>}
        {!!candidate?.features.length && <label><input type="checkbox" checked={visibility[uploadedRun ? "uploaded" : "candidate"]} onChange={(event) => setVisible(uploadedRun ? "uploaded" : "candidate", event.target.checked)} />{t(uploadedRun ? "Uploaded candidates" : "Candidate facilities")}</label>}
        {uploaded.count > 0 && <><label><input type="checkbox" checked={uploaded.visible} onChange={(event) => uploaded.setVisible(event.target.checked)} />{t("Uploaded candidates")} ({uploaded.count})</label>
          <button type="button" className="button--secondary" disabled={!ready} onClick={uploaded.zoom}>{t("Zoom to uploaded points")}</button></>}</div>
    </details>}
  </>;
}
