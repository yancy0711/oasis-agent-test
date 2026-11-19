import { useCallback, useEffect, type RefObject } from "react";
import { LngLatBounds, Popup, type Map, type MapLayerMouseEvent } from "maplibre-gl";
import type { FeatureCollection, Point } from "geojson";
import { useAppStore } from "../../store/appStore";
import { useWorkspacePreferences } from "../../app/WorkspacePreferences";
import type { UploadedPoint } from "../../store/appStore.types";
import { MAP_LAYER_IDS } from "./mapConfig";

export const uploadedCandidateLayer = "uploaded-candidates";
const empty: UploadedPoint[] = [];

export function useUploadedCandidates(mapRef: RefObject<Map | null>, ready: boolean, interactive = true, enabled = true) {
  const { t } = useWorkspacePreferences();
  const points = useAppStore((state) => enabled ? state.uploadedPoints : empty);
  const visible = useAppStore((state) => state.studyLayerVisibility.uploaded);
  const setLayerVisibility = useAppStore((state) => state.setStudyLayerVisibility);
  const setVisible = useCallback((checked: boolean) => setLayerVisibility("uploaded", checked), [setLayerVisibility]);
  const unnamed = t("Uploaded candidate");

  const zoom = useCallback(() => {
    const map = mapRef.current;
    if (!map || !points.length || !ready) return;
    const bounds = new LngLatBounds();
    points.forEach(([longitude, latitude]) => bounds.extend([longitude, latitude]));
    setVisible(true);
    map.fitBounds(bounds, { padding: 60, maxZoom: 14, duration: 350 });
  }, [mapRef, points, ready, setVisible]);

  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map || !points.length) return;
    const data: FeatureCollection<Point> = {
      type: "FeatureCollection",
      features: points.map(([longitude, latitude, name]) => ({
        type: "Feature", geometry: { type: "Point", coordinates: [longitude, latitude] },
        properties: { name },
      })),
    };
    map.addSource(uploadedCandidateLayer, { type: "geojson", data });
    map.addLayer({
      id: uploadedCandidateLayer, type: "circle", source: uploadedCandidateLayer,
      paint: { "circle-radius": 7, "circle-color": "#a277e8", "circle-opacity": 0.45,
        "circle-stroke-width": 2, "circle-stroke-color": "#7940c9" },
    }, map.getLayer(MAP_LAYER_IDS.sites) ? MAP_LAYER_IDS.sites : undefined);
    return () => {
      if (!map.style) return;
      if (map.getLayer(uploadedCandidateLayer)) map.removeLayer(uploadedCandidateLayer);
      if (map.getSource(uploadedCandidateLayer)) map.removeSource(uploadedCandidateLayer);
    };
  }, [mapRef, points, ready]);

  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !points.length || !map?.getLayer(uploadedCandidateLayer)) return;
    map.setLayoutProperty(uploadedCandidateLayer, "visibility", visible ? "visible" : "none");
    if (!visible || !interactive) return;
    let popup: Popup | undefined;
    const click = (event: MapLayerMouseEvent) => {
      if (map.getLayer(MAP_LAYER_IDS.sites) && map.queryRenderedFeatures(event.point, { layers: [MAP_LAYER_IDS.sites] }).length) return;
      const feature = event.features?.[0];
      if (!feature || feature.geometry.type !== "Point") return;
      popup?.remove();
      popup = new Popup({ offset: 10 }).setLngLat([feature.geometry.coordinates[0], feature.geometry.coordinates[1]])
        .setText(feature.properties?.name || unnamed).addTo(map);
    };
    const enter = () => { map.getCanvas().style.cursor = "pointer"; };
    const leave = () => { map.getCanvas().style.cursor = ""; };
    map.on("click", uploadedCandidateLayer, click);
    map.on("mousemove", uploadedCandidateLayer, click);
    map.on("mouseenter", uploadedCandidateLayer, enter);
    const clear = () => { leave(); popup?.remove(); };
    map.on("mouseleave", uploadedCandidateLayer, clear);
    return () => {
      popup?.remove();
      map.off("click", uploadedCandidateLayer, click);
      map.off("mousemove", uploadedCandidateLayer, click);
      map.off("mouseenter", uploadedCandidateLayer, enter);
      map.off("mouseleave", uploadedCandidateLayer, clear);
    };
  }, [mapRef, points, ready, visible, interactive, unnamed]);

  return { count: points.length, visible, setVisible, zoom };
}
