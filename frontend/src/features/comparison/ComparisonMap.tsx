import { useEffect, useRef, useState } from "react";
import type { FeatureCollection } from "geojson";
import maplibregl, { type CircleLayerSpecification, type FillLayerSpecification, type LineLayerSpecification, type Map as MapLibreMap } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { BASE_MAP_STYLE, HEAT_COLORS } from "../result/mapConfig";
import { popupContent } from "../result/mapPopup";

interface ComparisonDataset extends FeatureCollection {
  metadata: {
    renderer: "maplibre";
    value_range: [number, number] | null;
    bounds: [[number, number], [number, number]];
  };
}

interface ComparisonMapProps {
  url?: string;
  baselineUrl?: string;
  alternativeUrl?: string;
  baselineLabel: string;
  alternativeLabel: string;
}

const BASE_SOURCE = "comparison-base";
const BASELINE_SOURCE = "comparison-baseline";
const ALTERNATIVE_SOURCE = "comparison-alternative";
const BASELINE_LAYER = "comparison-baseline-sites";
const ALTERNATIVE_LAYER = "comparison-alternative-sites";

function collection(features: FeatureCollection["features"]): FeatureCollection {
  return { type: "FeatureCollection", features };
}

function pointKey(feature: FeatureCollection["features"][number]): string | null {
  if (feature.geometry.type !== "Point") return null;
  const [longitude, latitude] = feature.geometry.coordinates;
  return `${Number(longitude).toFixed(5)}:${Number(latitude).toFixed(5)}`;
}

async function loadDataset(url: string, signal: AbortSignal): Promise<ComparisonDataset> {
  const response = await fetch(url, { signal });
  if (!response.ok) throw new Error(`Comparison map request failed (${response.status})`);
  const data = await response.json() as ComparisonDataset;
  if (data.type !== "FeatureCollection" || data.metadata?.renderer !== "maplibre") {
    throw new Error("The comparison map artifact is not valid MapLibre data.");
  }
  return data;
}

export function ComparisonMap({ url, baselineUrl, alternativeUrl, baselineLabel, alternativeLabel }: ComparisonMapProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const [datasets, setDatasets] = useState<ComparisonDataset[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const urls = url ? [url] : baselineUrl && alternativeUrl ? [baselineUrl, alternativeUrl] : [];
    if (!urls.length) { setDatasets(null); return; }
    const controller = new AbortController();
    setError(null);
    Promise.all(urls.map((value) => loadDataset(value, controller.signal)))
      .then(setDatasets)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Could not load comparison map data.");
      });
    return () => controller.abort();
  }, [url, baselineUrl, alternativeUrl]);

  useEffect(() => {
    if (!datasets?.length || !containerRef.current) return;
    const [base, alternativeData] = datasets;
    const singleArtifact = datasets.length === 1;
    const baseFeatures = base.features.filter((feature) => feature.properties?.layer_kind !== "selected_site" && feature.properties?.layer_kind !== "comparison_site");
    const rawBaselineSites = base.features.filter((feature) => singleArtifact
      ? feature.properties?.layer_kind === "comparison_site" && feature.properties?.comparison_role === "baseline"
      : feature.properties?.layer_kind === "selected_site");
    const alternativeSites = (singleArtifact ? base : alternativeData).features.filter((feature) => singleArtifact
      ? feature.properties?.layer_kind === "comparison_site" && feature.properties?.comparison_role === "alternative"
      : feature.properties?.layer_kind === "selected_site");
    const alternativeKeys = new Set(alternativeSites.map(pointKey).filter((value): value is string => Boolean(value)));
    const baselineSites = rawBaselineSites.filter((feature) => {
      const key = pointKey(feature);
      return !key || !alternativeKeys.has(key);
    });
    baselineSites.forEach((feature) => { if (feature.properties) Object.assign(feature.properties, { comparison_label: baselineLabel }); });
    alternativeSites.forEach((feature) => { if (feature.properties) Object.assign(feature.properties, { comparison_label: alternativeLabel }); });

    const map = new maplibregl.Map({ container: containerRef.current, style: BASE_MAP_STYLE, attributionControl: false });
    mapRef.current = map;
    const resizeObserver = new ResizeObserver(() => map.resize());
    resizeObserver.observe(containerRef.current);
    map.addControl(new maplibregl.NavigationControl({ showCompass: true }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");
    map.addControl(new maplibregl.AttributionControl({ compact: true }), "bottom-right");
    let popup: maplibregl.Popup | null = null;
    let pinned = false;

    map.on("load", () => {
      const range = base.metadata.value_range;
      const min = range?.[0] ?? 0;
      const max = range?.[1] && range[1] > min ? range[1] : min + 1;
      const span = max - min;
      map.addSource(BASE_SOURCE, { type: "geojson", data: collection(baseFeatures) });
      map.addSource(BASELINE_SOURCE, { type: "geojson", data: collection(baselineSites) });
      map.addSource(ALTERNATIVE_SOURCE, { type: "geojson", data: collection(alternativeSites) });
      const tracts: FillLayerSpecification = { id: "comparison-tracts", type: "fill", source: BASE_SOURCE, filter: ["==", ["get", "layer_kind"], "tract"], paint: { "fill-color": ["interpolate", ["linear"], ["coalesce", ["get", "indicator_value"], min], min, HEAT_COLORS[0], min + span * .4, HEAT_COLORS[1], min + span * .72, HEAT_COLORS[2], max, HEAT_COLORS[3]], "fill-opacity": .58 } };
      const outlines: LineLayerSpecification = { id: "comparison-tract-outlines", type: "line", source: BASE_SOURCE, filter: ["==", ["get", "layer_kind"], "tract"], paint: { "line-color": "#4c514e", "line-width": .55 } };
      const boundary: LineLayerSpecification = { id: "comparison-boundary", type: "line", source: BASE_SOURCE, filter: ["==", ["get", "layer_kind"], "boundary"], paint: { "line-color": "#2455ff", "line-width": 2, "line-dasharray": [3, 3] } };
      const baseline: CircleLayerSpecification = { id: BASELINE_LAYER, type: "circle", source: BASELINE_SOURCE, paint: { "circle-radius": 8, "circle-color": "#2455ff", "circle-stroke-color": "#ffffff", "circle-stroke-width": 2 } };
      const alternative: CircleLayerSpecification = { id: ALTERNATIVE_LAYER, type: "circle", source: ALTERNATIVE_SOURCE, paint: { "circle-radius": 8, "circle-color": "#f28e2b", "circle-stroke-color": "#ffffff", "circle-stroke-width": 2 } };
      [tracts, outlines, boundary, baseline, alternative].forEach((layer) => map.addLayer(layer));
      map.fitBounds(base.metadata.bounds, { padding: 28, duration: 0, maxZoom: 13 });

      const showPopup = (event: Pick<maplibregl.MapMouseEvent, "lngLat"> & { features?: maplibregl.MapGeoJSONFeature[] }, pin: boolean) => {
        const feature = event.features?.[0]; if (!feature) return;
        popup?.remove(); pinned = pin;
        const coordinates = feature.geometry.type === "Point" ? feature.geometry.coordinates : [event.lngLat.lng, event.lngLat.lat];
        const point: [number, number] = [Number(coordinates[0]), Number(coordinates[1])];
        popup = new maplibregl.Popup({ closeButton: pin, closeOnClick: false, offset: 10 })
          .setLngLat(point).setDOMContent(popupContent(feature.properties ?? {}, point, pin)).addTo(map);
        popup.on("close", () => { popup = null; pinned = false; });
      };
      [BASELINE_LAYER, ALTERNATIVE_LAYER].forEach((layerId) => {
        map.on("mousemove", layerId, (event) => { if (!pinned) showPopup(event, false); });
        map.on("mouseleave", layerId, () => { if (!pinned) { popup?.remove(); popup = null; } });
        map.on("mouseenter", layerId, () => { map.getCanvas().style.cursor = "pointer"; });
        map.on("mouseleave", layerId, () => { map.getCanvas().style.cursor = ""; });
      });
      map.on("click", (event) => {
        if (pinned) { popup?.remove(); popup = null; pinned = false; return; }
        const features = map.queryRenderedFeatures(event.point, { layers: [ALTERNATIVE_LAYER, BASELINE_LAYER] });
        if (features[0]) showPopup({ ...event, features: [features[0]] }, true);
      });
    });
    return () => { resizeObserver.disconnect(); popup?.remove(); map.remove(); mapRef.current = null; };
  }, [datasets, baselineLabel, alternativeLabel]);

  if (error) return <p className="error error--block">{error}</p>;
  if (!datasets) return <p className="muted">No comparison map is available for these results.</p>;
  return <section className="comparison-map-shell">
    <div className="comparison-map-legend"><span><i className="is-baseline" />{baselineLabel}</span><span><i className="is-alternative" />{alternativeLabel}</span><small>Shared locations use the current marker; blue marks baseline-only selections.</small></div>
    <div ref={containerRef} className="comparison-map" aria-label="Candidate facility comparison map" />
  </section>;
}
