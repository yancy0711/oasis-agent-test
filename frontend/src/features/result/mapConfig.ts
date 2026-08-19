import type { FillLayerSpecification, LineLayerSpecification, MapOptions, CircleLayerSpecification } from "maplibre-gl";

export const MAP_SOURCE_ID = "oasis-results";
export const ROUTE_SOURCE_ID = "oasis-planned-route";
export const PARETO_SOURCE_ID = "oasis-pareto-preview";
export const MAP_LAYER_IDS = {
  tracts: "oasis-tracts",
  tractOutline: "oasis-tract-outlines",
  boundary: "oasis-boundary",
  sites: "oasis-selected-sites",
  route: "oasis-planned-route",
  routeOrigin: "oasis-route-origin",
  paretoSites: "oasis-pareto-sites",
} as const;

export const BASE_MAP_STYLE: MapOptions["style"] = {
  version: 8,
  sources: {
    carto: {
      type: "raster",
      tiles: ["https://basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png"],
      tileSize: 256,
      attribution: "© OpenStreetMap contributors · © CARTO",
    },
  },
  layers: [{ id: "carto", type: "raster", source: "carto" }],
};

export const HEAT_COLORS = ["#fff7bc", "#fec44f", "#f03b20", "#8c2d04"] as const;

export function resultLayers(range: [number, number] | null) {
  const min = range?.[0] ?? 0;
  const max = range?.[1] && range[1] > min ? range[1] : min + 1;
  const span = max - min;
  const tracts: FillLayerSpecification = {
    id: MAP_LAYER_IDS.tracts, type: "fill", source: MAP_SOURCE_ID,
    filter: ["==", ["get", "layer_kind"], "tract"],
    paint: {
      "fill-color": ["interpolate", ["linear"], ["coalesce", ["get", "indicator_value"], min],
        min, HEAT_COLORS[0], min + span * 0.4, HEAT_COLORS[1],
        min + span * 0.72, HEAT_COLORS[2], max, HEAT_COLORS[3]],
      "fill-opacity": ["case", ["==", ["get", "indicator_value"], null], 0, 0.72],
    },
  };
  const tractOutline: LineLayerSpecification = {
    id: MAP_LAYER_IDS.tractOutline, type: "line", source: MAP_SOURCE_ID,
    filter: ["==", ["get", "layer_kind"], "tract"],
    paint: {
      "line-color": ["case", ["==", ["get", "is_hardest_reachability"], true], "#2455ff", "#4c514e"],
      "line-width": ["case", ["==", ["get", "is_hardest_reachability"], true], 4, 0.55],
      "line-opacity": ["case", ["==", ["get", "is_hardest_reachability"], true], 1, 0.7],
    },
  };
  const boundary: LineLayerSpecification = {
    id: MAP_LAYER_IDS.boundary, type: "line", source: MAP_SOURCE_ID,
    filter: ["==", ["get", "layer_kind"], "boundary"],
    paint: { "line-color": "#2455ff", "line-width": 2, "line-dasharray": [3, 3] },
  };
  const sites: CircleLayerSpecification = {
    id: MAP_LAYER_IDS.sites, type: "circle", source: MAP_SOURCE_ID,
    filter: ["==", ["get", "layer_kind"], "selected_site"],
    paint: { "circle-radius": 8, "circle-color": "#2455ff",
      "circle-stroke-color": "#ffffff", "circle-stroke-width": 2.5 },
  };
  return [tracts, tractOutline, boundary, sites];
}

export function plannedRouteLayers() {
  const route: LineLayerSpecification = {
    id: MAP_LAYER_IDS.route, type: "line", source: ROUTE_SOURCE_ID,
    filter: ["==", ["get", "layer_kind"], "planned_route"],
    paint: { "line-color": "#7b2cbf", "line-width": 4, "line-opacity": 0.88 },
  };
  const origin: CircleLayerSpecification = {
    id: MAP_LAYER_IDS.routeOrigin, type: "circle", source: ROUTE_SOURCE_ID,
    filter: ["==", ["get", "layer_kind"], "route_origin"],
    paint: { "circle-radius": ["case", ["==", ["get", "outside_study_area"], true], 10, 7],
      "circle-color": ["case", ["==", ["get", "outside_study_area"], true], "#c43d2f", "#f57c00"],
      "circle-stroke-color": "#ffffff", "circle-stroke-width": 2 },
  };
  return [route, origin];
}

export const paretoPreviewLayer: CircleLayerSpecification = {
  id: MAP_LAYER_IDS.paretoSites, type: "circle", source: PARETO_SOURCE_ID,
  paint: { "circle-radius": 11, "circle-color": "#e4a53c",
    "circle-stroke-color": "#473721", "circle-stroke-width": 3 },
};
