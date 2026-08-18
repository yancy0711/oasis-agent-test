import { getStreetView } from "../../api/client";

function popupRow(label: string, value: unknown) {
  if (value === null || value === undefined || value === "") return null;
  const row = document.createElement("div");
  const term = document.createElement("strong");
  term.textContent = `${label}: `;
  const text = String(value);
  if (label === "Website" && /^(?:https?:\/\/|www\.)/i.test(text)) {
    const link = document.createElement("a");
    link.href = /^https?:\/\//i.test(text) ? text : `https://${text}`;
    link.textContent = text;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    row.append(term, link);
  } else row.append(term, document.createTextNode(text));
  return row;
}

function temperature(value: unknown) {
  return `${Number(value).toFixed(1)} °C`;
}

function appendStreetView(root: HTMLElement, longitude: number, latitude: number) {
  const panel = document.createElement("section");
  panel.className = "map-popup__streetview";
  const status = document.createElement("p");
  status.className = "map-popup__streetview-status map-popup__streetview-status--notice";
  status.textContent = "Checking for nearby street-level imagery...";
  panel.append(status);
  root.append(panel);
  void getStreetView(latitude, longitude).then((result) => {
    panel.replaceChildren();
    if (!result.available || !result.image_url) {
      status.className = "map-popup__streetview-status map-popup__streetview-status--error";
      status.textContent = result.message || "No nearby street-level imagery is available.";
      panel.append(status);
      return;
    }
    const link = document.createElement("a");
    link.href = result.viewer_url || result.image_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    const image = document.createElement("img");
    image.src = result.image_url;
    image.alt = "Nearest Mapillary street-level view";
    image.loading = "lazy";
    image.addEventListener("error", () => {
      panel.replaceChildren();
      status.className = "map-popup__streetview-status map-popup__streetview-status--error";
      status.textContent = "The nearby image could not be displayed.";
      panel.append(status);
    });
    link.append(image);
    const caption = document.createElement("small");
    const distance = result.distance_m == null ? "" : ` | ${Math.round(result.distance_m)} m away`;
    const capturedDate = result.captured_at ? new Date(result.captured_at) : null;
    const captured = capturedDate && !Number.isNaN(capturedDate.valueOf())
      ? ` | captured ${capturedDate.toLocaleDateString()}` : "";
    caption.textContent = `Mapillary image${distance}${captured}${result.creator ? ` | ${result.creator}` : ""}`;
    panel.append(link, caption);
  }).catch((reason: unknown) => {
    status.className = "map-popup__streetview-status map-popup__streetview-status--error";
    status.textContent = reason instanceof DOMException && reason.name === "TimeoutError"
      ? "Street-level imagery check timed out. Please try the selected facility again."
      : reason instanceof Error ? reason.message : "Street-level imagery could not be checked.";
  });
}

export function popupContent(
  properties: Record<string, unknown>,
  coordinates?: [number, number],
  includeStreetView = true,
) {
  const root = document.createElement("div");
  root.className = "map-popup";
  const route = properties.layer_kind === "planned_route";
  const paretoSite = properties.layer_kind === "pareto_preview_site";
  const selectedSite = properties.layer_kind === "selected_site" || properties.layer_kind === "comparison_site";
  const heading = document.createElement("strong");
  heading.textContent = String(properties.name ?? (paretoSite ? "Pareto preview site" : route
    ? `Route to ${properties.destination_name ?? properties.destination_id ?? "selected candidate facility"}`
    : `Census tract ${properties.GEOID ?? ""}`));
  root.append(heading);
  const rows = paretoSite ? [
    ["Candidate ID", properties.candidate_id], ["Preview alpha", properties.alpha],
    ["Status", "Temporary preview; the current recommendation is unchanged"],
  ] : selectedSite ? [
    ["Comparison", properties.comparison_label], ["Candidate ID", properties.candidate_id],
    ["Facility type", properties.facility_type ?? "Unknown"], ["Mobility", properties.mobility],
    ["Public access", properties.public_access], ["Cooling service", properties.cooling_service],
    ["Opening hours", properties.opening_hours ?? "Unknown"], ["Fee", properties.fee ?? "Unknown"],
    ["Window feasibility", properties.hours_label], ["Requested window", properties.requested_window],
    ["Website", properties.website],
    ["Observed capacity", properties.observed_capacity_people == null ? null : `${properties.observed_capacity_people} people`],
    ["OSM polygon footprint area", properties.footprint_area_m2 == null ? null : `${Number(properties.footprint_area_m2).toFixed(0)} m²`],
    ["Capacity evidence", properties.capacity_evidence],
  ] : route ? [
    ["Origin tract", properties.origin_geoid], ["Selected candidate facility", properties.destination_id],
    ["Selection", properties.selection_method], ["Mode", properties.mode],
    ["Duration", properties.duration_minutes == null ? null : `${Number(properties.duration_minutes).toFixed(1)} min`],
    ["Distance", properties.distance_km == null ? null : `${Number(properties.distance_km).toFixed(2)} km`],
    ["Route geometry", properties.geometry_source], ["Route cache", properties.cache_tier],
    ["Route mean LST", properties.route_mean_lst_c == null ? null : temperature(properties.route_mean_lst_c)],
    ["Route mean NDVI", properties.route_mean_ndvi], ["Route mean NDBI", properties.route_mean_ndbi],
    ["Route mean albedo proxy", properties.route_mean_albedo],
  ] : [
    ["Population", properties.population], ["Age 65+", properties.elderly_pct == null ? null : `${properties.elderly_pct}%`],
    ["Poverty", properties.poverty_pct == null ? null : `${properties.poverty_pct}%`],
    ["Population-weighted LST", properties.population_weighted_heat_c == null ? null : temperature(properties.population_weighted_heat_c)],
    ["Tract mean LST", properties.tract_mean_heat_c == null ? null : temperature(properties.tract_mean_heat_c)],
    ["Difference from tract mean", properties.heat_exposure_gap_c == null ? null : temperature(properties.heat_exposure_gap_c)],
    ["Population with valid LST", properties.valid_heat_population_pct == null ? null : `${properties.valid_heat_population_pct}%`],
    ["Temperature calculation", properties.heat_exposure_method],
    ["Vegetation index (NDVI)", properties.mean_ndvi], ["Built-up index (NDBI)", properties.mean_ndbi],
    ["Broadband albedo proxy", properties.mean_albedo],
    ["Nearest candidate facility", properties.nearest_candidate_minutes == null ? null : `${Number(properties.nearest_candidate_minutes).toFixed(1)} min`],
    ["Candidates within threshold", properties.reachable_candidates],
    ["Reachability rank", properties.reachability_rank],
    ["Diagnostic marker", properties.is_hardest_reachability ? "Hardest tract in this review" : null],
  ];
  rows.forEach(([label, value]) => { const row = popupRow(String(label), value); if (row) root.append(row); });
  if (includeStreetView && (selectedSite || paretoSite) && coordinates) {
    appendStreetView(root, coordinates[0], coordinates[1]);
  }
  return root;
}
