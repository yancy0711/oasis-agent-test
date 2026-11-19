import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import type {
  ParseResponse, PlaceConfirmResponse, PlaceResolveResponse, RunAccepted,
  RunRequest, RunResult, RunSnapshot, TaskSpec, UiConfigResponse,
} from "../src/api/models";

const task: TaskSpec = {
  place_name: "Phoenix, Arizona, USA",
  place_names: ["Phoenix, Arizona, USA"],
  intervention: "cooling_center",
  analysis_goal: "facility_siting",
  result_queries: [],
  ambiguities: [],
  candidate_source: "osm",
  mobility_scenario: "walking",
  recommendation_rationale: "",
  n_facilities: 8,
  threshold_minutes: 15,
  coverage_group: "all",
  weights: {
    population_density: 0.35, elderly_ratio: 0.15, poverty_rate: 0.15,
    heat_exposure: 0.2, environment_burden: 0.15,
  },
  candidate_amenities: ["library", "community_centre"],
  regional_requirements: [],
  sensitivity_thresholds: [],
  sensitivity_facility_counts: [],
  comparison_profiles: [],
  recommended_sensitivity_thresholds: [],
  recommended_sensitivity_facility_counts: [],
  preference_question: "",
  notes: "",
};

const config: UiConfigResponse = {
  optimization: { n_facilities: 8 },
  accessibility: { threshold_minutes: 15 },
  agent: { provider: "openrouter", model: "test-model", base_url: "https://openrouter.ai/api/v1" },
  execution: { modes: ["auto", "quick", "precise"], default_mode: "auto" },
  value_profiles: { balanced: { label: "Balanced", weights: task.weights } },
  carto_basemap_api_key: "",
};

const place: PlaceConfirmResponse = {
  candidate_id: "candidate-1",
  canonical_name: "Phoenix, Arizona, USA",
  display_name: "Phoenix, Maricopa County, Arizona, USA",
  place_type: "city",
  label: "Phoenix (city)",
  query: task.place_name,
};

const result: RunResult = {
  ok: true, paused: false, message: "The recommended cooling-center plan is ready.",
  task, artifacts: { map: "/api/v1/runs/run-1/artifacts/map", report: "/api/v1/runs/run-1/artifacts/report" },
  execution: { mode: "auto", phase: "quick", status: "complete", accuracy: "approximate", router_requested: "haversine", router_used: "haversine" },
  metrics: { coverage: 0.63, gradual_coverage: 0.38, avg_walk: 12.8, vulnerable: 0.72, population_weighted_access_gini: 0.26 },
  lineage: [{ source: "Test census fixture", layer: "Population" }],
};
const map = {
  type: "FeatureCollection",
  metadata: { renderer: "maplibre", indicator: "heat_exposure", indicator_label: "Land surface temperature (°C)", value_range: [30, 46], bounds: [[-112.16, 33.39], [-112.00, 33.53]] },
  features: [
    ...Array.from({ length: 18 }, (_, index) => {
      const x = -112.16 + (index % 6) * 0.027;
      const y = 33.39 + Math.floor(index / 6) * 0.046;
      return { type: "Feature", geometry: { type: "Polygon", coordinates: [[[x, y], [x + 0.027, y], [x + 0.027, y + 0.046], [x, y + 0.046], [x, y]]] }, properties: { layer_kind: "tract", GEOID: String(index), indicator_value: 30 + (index * 7) % 17 } };
    }),
    ...Array.from({ length: 8 }, (_, index) => ({
      type: "Feature", geometry: { type: "Point", coordinates: [-112.15 + (index % 4) * 0.04, 33.42 + Math.floor(index / 4) * 0.07] },
      properties: { layer_kind: "selected_site", name: `Candidate site ${index + 1}`, candidate_id: `candidate-${index + 1}` },
    })),
  ],
};
const stagedTracts = { type: "FeatureCollection", features: map.features.filter((feature) => feature.properties.layer_kind === "tract") };
const stagedBoundary = { type: "FeatureCollection", features: [{
  ...stagedTracts.features[0], properties: { layer_kind: "boundary" },
}] };

test("regional workflow fits the narrow workspace, accepts map drops, and keeps confirmed uploads ahead of providers", async ({ page }) => {
  const hongKong = "Kowloon City, Hong Kong";
  const shenzhen = "Nanshan District, Shenzhen, China";
  const uploadId = "0123456789abcdef0123456789abcdef";
  const uploadText = readFileSync(new URL("../../docs/examples/hong-kong-demand.geojson", import.meta.url), "utf8");
  const geometry = JSON.parse(uploadText);
  let studyArea = hongKong;
  let hkChecks = 0;
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.setViewportSize({ width: 860, height: 950 });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const endpoint = new URL(request.url()).pathname;
    const send = (json: unknown, status = 200) => route.fulfill({ json, status });
    const resolved = () => ({ ...place, canonical_name: studyArea, display_name: studyArea, place_type: "district",
      candidate_id: studyArea === hongKong ? "hk:test" : "sz:test" });
    switch (endpoint) {
      case "/api/v1/config": return send({ ...config, utci_configured: true });
      case "/api/v1/requests/interpret": return send({ action: "select_facilities", workflow: "research", reason: "Plan an area", preferences: null });
      case "/api/v1/parse":
        studyArea = request.postDataJSON().prompt.includes("Shenzhen") ? shenzhen : hongKong;
        return send({ disposition: "full_analysis", task: { ...task, n_facilities: 3, place_name: studyArea,
          place_names: [studyArea], recommended_value_profile: "balanced",
          recommendation_rationale: "Prioritize residents aged 65 and over; review the proposed walking scenario and vulnerability weights." },
        missing_place: false, reason: "Regional siting", alternative: "" });
      case "/api/v1/places/resolve":
        return send({ resolution_id: "district", query: studyArea, ambiguous: false, candidates: [resolved()], selected: resolved() });
      case "/api/v1/places/confirm": return send(resolved());
      case "/api/v1/regional-data":
        if (new URL(request.url()).searchParams.get("place_name") === shenzhen) return send({ detail: "Provider unavailable" }, 503);
        hkChecks += 1;
        return send({ place_name: hongKong, year: 2021, units: 18, older_units: 18, source: "HK Census",
          source_url: "https://example.com", geometry });
      case "/api/v1/regional-data/download":
        return send({ ...geometry, features: geometry.features.slice(0, 1) });
      case "/api/v1/uploads/demand":
        expect(request.postData()).toContain("hong-kong-demand.geojson");
        return send({ upload_id: uploadId, place_name: hongKong, units: 6, filename: "hong-kong-demand.geojson",
          fields: { older: "elderly_population" }, geometry, boundary: { ...geometry, features: geometry.features.slice(0, 1) } });
      default: return send({}, 404);
    }
  });
  await page.goto("/");
  const composer = page.getByRole("textbox", { name: "Describe the cooling-access question" });
  const analyze = async (prompt: string) => {
    await composer.fill(prompt);
    await page.getByRole("button", { name: "Analyze request" }).click();
  };
  await analyze("喺九龍城區揀 3 個避暑中心，行路 15 分鐘內，優先照顧 65 歲或以上嘅居民。");
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await page.getByRole("button", { name: /Kowloon City, Hong Kong/ }).click();
  const recommendation = page.getByRole("button", { name: "Apply all recommendations" });
  await expect(recommendation).toBeVisible();
  const [buttonBox, leftBox] = await Promise.all([
    recommendation.boundingBox(), page.locator("#workspace-inspector").boundingBox(),
  ]);
  expect(buttonBox!.x + buttonBox!.width).toBeLessThanOrEqual(leftBox!.x + leftBox!.width);
  await expect.poll(() => hkChecks).toBe(1);

  const drag = await page.evaluateHandle((content) => {
    const payload = new DataTransfer();
    payload.items.add(new File([content], "hong-kong-demand.geojson", { type: "application/geo+json" }));
    return payload;
  }, uploadText);
  await page.locator(".workbench-map").dispatchEvent("drop", { dataTransfer: drag });
  await page.getByRole("button", { name: "Confirm and use uploaded demand" }).click();
  await expect(page.getByRole("checkbox", { name: "Demand zones (6)" })).toBeChecked();
  await expect(page.getByRole("checkbox", { name: "Study boundary", exact: true })).toBeChecked();

  await analyze("Select three cooling centres in Kowloon City, Hong Kong within a 15-minute walk.");
  await expect(page.getByText("The confirmed upload supplies the study boundary and population.")).toBeVisible();
  expect(hkChecks).toBe(1);

  await analyze("Select three cooling centres in Nanshan District, Shenzhen, China within a 15-minute walk.");
  await page.getByRole("button", { name: /Nanshan District, Shenzhen/ }).click();
  const setup = page.getByRole("tabpanel", { name: "Setup" });
  await expect(setup.getByText("Check the regional population layer in Data or upload demand polygons.")).toBeVisible();
  await expect(setup.getByRole("spinbutton", { name: "Facilities", exact: true })).toHaveValue("3");
  await setup.getByRole("button", { name: "Upload demand data" }).click();
  await expect(page.getByRole("link", { name: "Download study boundary" })).toBeVisible();
  expect(pageErrors).toEqual([]);
});

// Exercises browser/API contracts with fixtures in place of external providers.
async function exerciseRun(page: Page, outcome: "cancelled" | "succeeded", pilot = false, configured = true) {
  let status: RunSnapshot["status"] = "running";
  const placeName = pilot ? "New York, New York, USA" : task.place_name;
  const resolvedPlace = pilot ? { ...place, canonical_name: placeName, display_name: placeName, query: placeName } : place;
  const parsedTask = { ...task, place_name: placeName, place_names: [placeName] };
  let confirmedTask: TaskSpec = parsedTask;
  let startCount = 0;
  const unexpectedRequests: string[] = [];
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));

  function snapshot(): RunSnapshot {
    return {
      run_id: "run-1", parent_run_id: null, revision: 0, status,
      phase: "quick", mode: "auto",
      spec: { task: confirmedTask, mode: "auto" }, task: confirmedTask,
      events: [{ seq: 1, kind: "agent", stage: "decision", message: "Resolve the confirmed area.", tool: "resolve_study_area", data: {} }],
      progress: status === "running" ? {
        kind: "progress", label: "Preparing demand", done: 1, total: 4,
        unit: "stages", fraction: 0.25, cache_hits: 0, remote_pairs: 0, inflight_pairs: 0,
        stage_elapsed_seconds: 1, last_activity_seconds: 0,
      } : null,
      preview_result: null, result: status === "succeeded" ? { ...result, task: confirmedTask } : null, error: null, cancel_reason: null,
      created_at: "2026-09-30T00:00:00Z", updated_at: "2026-09-30T00:00:01Z",
    };
  }

  await page.route((url) => url.pathname.startsWith("/api/"), async (route) => {
    const request = route.request();
    const endpoint = `${request.method()} ${new URL(request.url()).pathname}`;
    const json = (body: unknown, code = 200) => route.fulfill({
      status: code, contentType: "application/json", body: JSON.stringify(body),
    });

    switch (endpoint) {
      case "GET /api/v1/config":
        return json({ ...config, utci_configured: configured, utci_auto_download: pilot });
      case "POST /api/v1/llm/models":
        return json({ provider: "openrouter", models: ["openai/gpt-4o"] });
      case "POST /api/v1/requests/interpret":
        return json({ action: "select_facilities", workflow: "research", reason: "Facility portfolio analysis", preferences: null });
      case "POST /api/v1/parse":
        return json({
          disposition: "full_analysis", task: parsedTask, missing_place: false, message: "",
          reason: "Supported candidate-facility analysis.", alternative: "",
          events: [], route_origin_candidates: [], place_name_options: [],
        } satisfies ParseResponse);
      case "POST /api/v1/places/resolve":
        return json({
          resolution_id: "resolution-1", query: placeName,
          ambiguous: false, candidates: [resolvedPlace], selected: null,
        } satisfies PlaceResolveResponse);
      case "POST /api/v1/places/confirm":
        return json(resolvedPlace);
      case "POST /api/v1/uploads":
        return json({ filename: "candidates.csv", upload_id: "candidate-upload", size: 36, preview_points: [[-112.07, 33.45, "Library"]] });
      case "POST /api/v1/runs":
        startCount += 1;
        confirmedTask = (request.postDataJSON() as RunRequest).task;
        return json({ run_id: "run-1", status: "queued", mode: "auto" } satisfies RunAccepted, 202);
      case "GET /api/v1/runs/run-1":
        return json(snapshot());
      case "GET /api/v1/runs/run-1/input-layers":
        return json({ boundary: stagedBoundary, tract: pilot ? { ...stagedTracts, features: stagedTracts.features.map((feature) => ({
          ...feature, properties: { ...feature.properties, lst_exposure_c: 34, utci_c: 24 },
        })) } : stagedTracts });
      case "POST /api/v1/runs/run-1/cancel":
        status = "cancelled";
        return json(snapshot(), 202);
      case "GET /api/v1/runs/run-1/artifacts/map":
        if (!pilot) return json(map);
        return json({
          ...map, metadata: {
            ...map.metadata, run_heat_metric: confirmedTask.heat_metric, siting_heat_weight: .2,
            thermal_layers: {
              utci: { label: "UTCI", unit: "°C", source: "Copernicus", source_url: "https://doi.org/10.24381/cds.553b7518",
                start_date: "2022-06-01", end_date: "2022-08-31", resolution: "0.25° native grid", field: "utci_c", value_range: [22, 25] },
              landsat_lst: { label: "Landsat land-surface temperature", unit: "°C", source: "Landsat",
                start_date: "2022-06-01", end_date: "2022-08-31", resolution: "120 m analysis grid", field: "lst_exposure_c", value_range: [30, 46] },
            },
          },
          features: map.features.map((feature) => "GEOID" in feature.properties
            ? { ...feature, properties: { ...feature.properties, utci_c: 22 + Number(feature.properties.GEOID) % 4,
              lst_exposure_c: feature.properties.indicator_value } }
            : feature),
        });
      case "GET /api/v1/runs/run-1/artifacts/report":
        return route.fulfill({ contentType: "text/markdown", body: "# Cooling center report\n\nExample results for Phoenix." });
      case "GET /api/v1/data-sources/status":
        return json({ checked_at: "2026-09-30T00:00:00Z", sources: [], updates_available: false });
      case "GET /api/v1/official-resources":
        return json({ status: "not_applicable", resources: [] });
      case "GET /api/streetview":
        return json({ available: false, status: "not_configured", message: "No preview image in this fixture." });
      default:
        unexpectedRequests.push(endpoint);
        return json({ detail: `Unmocked endpoint: ${endpoint}` }, 501);
    }
  });

  const tab = (name: string) => page.getByRole("tab", { name, exact: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/");
  await expect(tab("Data")).toHaveAttribute("aria-selected", "true");
  if (outcome === "succeeded" && !pilot) {
    await page.screenshot({ path: test.info().outputPath("data-1440x900.png") });
    const collapse = page.getByRole("button", { name: "Collapse workspace panel" });
    await collapse.click();
    await expect(tab("Data")).toBeVisible();
    await page.getByRole("button", { name: "Expand workspace panel" }).click();
    await page.getByRole("button", { name: "Site 10 cooling centers in Phoenix" }).click();
    await expect(page.getByRole("textbox", { name: "Describe the cooling-access question" })).toHaveValue(/Select 10 candidate cooling/);
    await expect(tab("Data")).toHaveAttribute("aria-selected", "true");
    await page.getByRole("button", { name: "Use my own data" }).click();
    const upload = page.getByRole("tabpanel", { name: "Data", exact: true }).getByLabel("Candidate CSV");
    await expect(upload).toBeFocused();
    await upload.setInputFiles({ name: "candidates.csv", mimeType: "text/csv", buffer: Buffer.from("name,lat,lon\nLibrary,33.45,-112.07\n") });
    await expect(page.getByText("Uploaded: candidates.csv")).toBeVisible();
    await page.getByRole("button", { name: "Remove uploaded candidate CSV" }).click();
    await expect(page.getByText("Uploaded: candidates.csv")).toHaveCount(0);
    await expect(page.locator(".study-map").getByRole("checkbox", { name: "Uploaded candidates (1)" })).toHaveCount(0);
    await upload.setInputFiles({ name: "candidates.csv", mimeType: "text/csv", buffer: Buffer.from("name,lat,lon\nLibrary,33.45,-112.07\n") });
    const studyMap = page.locator(".study-map");
    const uploaded = studyMap.getByRole("checkbox", { name: "Uploaded candidates (1)" });
    await expect(uploaded).toBeChecked();
    await uploaded.uncheck();
    await expect(uploaded).not.toBeChecked();
    await studyMap.getByRole("button", { name: "Zoom to uploaded points" }).click();
    await expect(uploaded).toBeChecked();
    await expect(async () => {
      await studyMap.locator("canvas").click();
      await expect(studyMap.locator(".maplibregl-popup")).toHaveText(/Library/, { timeout: 600 });
    }).toPass({ timeout: 4500 });
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await expect(page.getByRole("dialog", { name: "Settings" })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
  }
  await page.getByRole("tab", { name: "Find a nearby facility" }).click();
  await page.getByRole("checkbox", { name: "Auto-detect task type" }).check();
  await page.getByRole("textbox", { name: "Describe the cooling-access question", exact: true })
    .fill(`Select 8 candidate cooling facilities in ${placeName} within a 15-minute walk.`);
  await page.getByRole("button", { name: "Analyze request" }).click();
  await expect(page.getByRole("tab", { name: "Research and planning" })).toHaveAttribute("aria-selected", "true");
  await expect(tab("Setup")).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("heading", { name: "Confirm the boundary" })).toBeVisible();
  const runButton = page.getByRole("button", { name: "Run confirmed agent" });
  await expect(runButton).toBeDisabled();
  await page.getByRole("button", { name: new RegExp(placeName) }).click();
  await expect(page.getByRole("spinbutton", { name: "Facilities", exact: true })).toHaveValue("8");
  await expect(page.getByRole("combobox", { name: "Mobility", exact: true })).toHaveValue("walking");
  await expect(runButton).toBeEnabled();
  await expect(page.getByRole("combobox", { name: "Heat input for next run" })).toHaveCount(0);
  if (!configured) {
    await runButton.click();
    await expect(tab("Data")).toHaveAttribute("aria-selected", "true");
    await expect(page.getByText("UTCI needs a data source. See Data for setup.")).toBeVisible();
    await expect(page.getByRole("link", { name: "Connect Copernicus CDS" })).toBeVisible();
    expect(startCount).toBe(0);
    return;
  }
  if (outcome === "succeeded" && !pilot) {
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.screenshot({ path: test.info().outputPath("setup-1280x800.png") });
    await expect(page.getByRole("combobox", { name: "Candidate source" })).toHaveValue("csv");
    await expect(runButton).toBeEnabled();
  }

  const startRequest = page.waitForRequest((request) =>
    request.method() === "POST" && new URL(request.url()).pathname === "/api/v1/runs");
  await runButton.click();
  expect((await startRequest).postDataJSON()).toMatchObject({
    mode: "auto", task: {
      place_name: placeName, n_facilities: 8,
      threshold_minutes: 15, mobility_scenario: "walking",
      heat_metric: "utci",
    },
  });
  if (outcome === "succeeded" && !pilot) expect((await startRequest).postDataJSON()).toMatchObject({ candidate_upload_id: "candidate-upload" });
  await expect(page.getByText("Preparing demand", { exact: true })).toBeVisible();
  await expect(page.getByText("Resolve the confirmed area.", { exact: true })).toBeHidden();
  await page.locator(".trace-panel summary").click();
  await expect(page.getByText("Resolve the confirmed area.", { exact: true })).toBeVisible();
  await expect(page.locator(".run-summary .status")).toHaveText("running");
  const stagedDemand = page.locator(".study-map").getByRole("checkbox", { name: "Demand zones (18)" });
  await expect(stagedDemand).toBeChecked();
  await stagedDemand.uncheck();
  if (pilot) {
    const stagedHeat = page.locator(".study-map").getByRole("combobox", { name: "Heat layer" });
    await expect(stagedHeat).toHaveValue("utci_c");
    await stagedHeat.selectOption("lst_exposure_c");
    await expect(stagedHeat).toHaveValue("lst_exposure_c");
  }

  if (outcome === "succeeded") {
    await page.locator(".trace-panel summary").click();
    status = outcome;
    await expect(tab("Results")).toHaveAttribute("aria-selected", "true");
    await expect(page.getByText("The recommended cooling-center plan is ready.")).toBeVisible();
    await expect(page.locator(".workbench-map-slot canvas")).toBeVisible();
    await expect(page.locator(".study-map canvas")).toHaveCount(0);
    await expect(page.getByText("Loading interactive map", { exact: false })).toHaveCount(0);
    const resultLayers = page.locator(".workbench-map-slot .map-layers");
    const demand = resultLayers.getByRole("checkbox", { name: "Demand zones", exact: true });
    await expect(demand).not.toBeChecked();
    await demand.check();
    await expect(page.getByRole("button", { name: "Collapse route planner" })).toBeVisible();
    const noOverlap = async () => {
      const [layersBox, plannerBox] = await Promise.all([resultLayers.boundingBox(), page.locator(".map-route-control").boundingBox()]);
      return Boolean(layersBox && plannerBox && layersBox.y + layersBox.height <= plannerBox.y);
    };
    await expect.poll(noOverlap).toBe(true);
    if (pilot) {
      const heatLayer = page.getByRole("combobox", { name: "Heat layer" });
      await expect(heatLayer).toHaveValue("utci");
      const canvas = page.locator(".workbench-map-slot canvas");
      await canvas.evaluate((element) => element.setAttribute("data-theme-test", "retained"));
      await page.getByRole("button", { name: "Switch to dark mode" }).click();
      await expect(canvas).toHaveAttribute("data-theme-test", "retained");
      await page.getByText("Source & dates", { exact: true }).click();
      await expect(page.getByText(/0.25° native grid/)).toBeVisible();
      await expect(page.getByText("Used in siting weights", { exact: true })).toBeVisible();
      await heatLayer.selectOption("landsat_lst");
      await expect(page.getByText("View only", { exact: true })).toBeVisible();
      await expect(page.getByText(/120 m analysis grid/)).toBeVisible();
      expect(startCount).toBe(1);
      expect(unexpectedRequests).toEqual([]);
      expect(pageErrors).toEqual([]);
      return;
    }
    const uploaded = resultLayers.getByRole("checkbox", { name: "Uploaded candidates (1)" });
    await expect(uploaded).toBeChecked();
    await uploaded.uncheck();
    await expect(uploaded).not.toBeChecked();
    await page.setViewportSize({ width: 1024, height: 768 });
    await expect.poll(noOverlap).toBe(true);
    await expect.poll(() => page.locator(".workbench-map-slot canvas").evaluate((canvas) => Math.abs(canvas.clientWidth - canvas.closest(".workbench-map")!.clientWidth))).toBeLessThan(3);
    await page.screenshot({ path: test.info().outputPath("results-1024x768.png") });
    await page.getByText("Selected sites (8)", { exact: true }).click();
    const site = page.getByRole("button", { name: "Candidate site 2", exact: true });
    await site.click();
    await expect(site).toHaveAttribute("aria-pressed", "true");
    await expect(page.locator(".maplibregl-popup")).toContainText("Candidate site 2");
    await page.locator(".maplibregl-popup-close-button").click();
    await expect(site).toHaveAttribute("aria-pressed", "false");
    await page.getByRole("button", { name: "Compare scenarios" }).click();
    await expect(page.getByRole("dialog", { name: "Compare scenarios" }).getByRole("button", { name: "Mobility modes" })).toBeVisible();
    await page.getByRole("button", { name: "Close compare scenarios" }).click();
    await tab("Report").click();
    await page.getByRole("button", { name: "Open run report" }).click();
    await expect(page.getByRole("dialog", { name: "Run report" }).getByRole("link", { name: "Download Markdown" })).toBeVisible();
    await page.keyboard.press("Escape");
    await page.getByText("Data provenance", { exact: true }).click();
    await expect(page.getByText("Test census fixture")).toBeVisible();
    await tab("Data").click();
    await page.getByText("Data checks & processing").click();
    await expect(page.getByText("Missing indicator values")).toBeVisible();
    expect(unexpectedRequests).toEqual([]);
    expect(pageErrors).toEqual([]);
    return;
  }
  await page.getByRole("button", { name: "Cancel run" }).click();
  await expect(page.locator(".run-summary .status")).toHaveText("cancelled");
  await expect(page.getByRole("button", { name: "Cancel run" })).toHaveCount(0);
  await expect(page.getByText("Preparing demand", { exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.locator(".run-summary .status")).toHaveText("cancelled");
  await expect(page.getByRole("button", { name: "Cancel run" })).toHaveCount(0);
  expect(unexpectedRequests).toEqual([]);
  expect(pageErrors).toEqual([]);
}

test("parse, confirm, start, poll, cancel, and reload preserve one run", ({ page }) => exerciseRun(page, "cancelled"));
test("show a successful result, report, and data checks in the workbench", ({ page }) => exerciseRun(page, "succeeded"));
test("default to regional UTCI in New York and preview LST without starting another run", ({ page }) => exerciseRun(page, "succeeded", true));
test("show the connection entry when UTCI has no data source", ({ page }) => exerciseRun(page, "cancelled", false, false));

test("resize both panes independently and preserve the UI preferences", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.route("**/api/v1/config", (route) => route.fulfill({ json: { ...config, sf_utci_configured: false } }));
  await page.route("**/api/v1/llm/models", (route) => route.fulfill({ json: { models: ["openai/gpt-4o"] } }));
  await page.goto("/");
  await expect(page.getByText(/No ERA5-HEAT source configured/)).toBeVisible();
  const workspace = page.getByRole("complementary", { name: "Workspace", exact: true });
  const assistant = page.getByRole("complementary", { name: "Assistant", exact: true });
  const left = page.getByRole("separator", { name: "Resize workspace panel" });
  const right = page.getByRole("separator", { name: "Resize assistant panel" });
  const originalLeft = (await workspace.boundingBox())!.width;
  const originalRight = (await assistant.boundingBox())!.width;

  const handle = (await left.boundingBox())!;
  await page.mouse.move(handle.x + handle.width / 2, handle.y + handle.height / 2);
  await page.mouse.down();
  await page.mouse.move(handle.x + handle.width / 2 + 110, handle.y + handle.height / 2, { steps: 4 });
  await page.mouse.up();
  await expect.poll(async () => (await workspace.boundingBox())!.width).toBeGreaterThan(originalLeft + 100);
  expect((await assistant.boundingBox())!.width).toBe(originalRight);
  const resizedLeft = (await workspace.boundingBox())!.width;

  const assistantHandle = (await right.boundingBox())!;
  await page.mouse.move(assistantHandle.x + assistantHandle.width / 2, assistantHandle.y + assistantHandle.height / 2);
  await page.mouse.down();
  await page.mouse.move(assistantHandle.x + assistantHandle.width / 2 - 65, assistantHandle.y + assistantHandle.height / 2, { steps: 4 });
  await page.mouse.up();
  await expect.poll(async () => (await assistant.boundingBox())!.width).toBeGreaterThan(originalRight + 60);
  const resizedRight = (await assistant.boundingBox())!.width;
  await right.focus();
  await page.keyboard.press("ArrowLeft");
  await expect.poll(async () => (await assistant.boundingBox())!.width).toBe(resizedRight + 16);
  expect((await workspace.boundingBox())!.width).toBe(resizedLeft);
  expect((await page.locator(".workbench-main").boundingBox())!.width).toBeGreaterThanOrEqual(300);

  const composer = page.getByRole("textbox", { name: "Describe the cooling-access question" });
  await expect(page.getByRole("button", { name: "Switch language" })).toHaveText("CN/EN");
  await composer.fill("Compare cooling access in Phoenix");
  await page.getByRole("button", { name: "Switch to dark mode" }).click();
  await page.getByRole("button", { name: "Switch language" }).click();
  await expect(page.locator("html")).toHaveAttribute("lang", "zh-CN");
  await expect(page.locator(".workbench")).toHaveAttribute("data-theme", "dark");
  await expect(page.getByRole("tab", { name: "数据" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "助手", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "切换语言" })).toHaveText("CN/EN");
  await expect(page.getByRole("textbox", { name: "描述你的避暑可达性问题" })).toHaveValue("Compare cooling access in Phoenix");
  await page.screenshot({ path: test.info().outputPath("workbench-dark-zh.png") });
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("lang", "zh-CN");
  await expect(page.locator(".workbench")).toHaveAttribute("data-theme", "dark");
  await page.setViewportSize({ width: 760, height: 950 });
  await expect(page.getByRole("separator", { name: "调整工作区宽度" })).toBeHidden();
  await expect(page.getByRole("heading", { name: "助手", exact: true })).toBeVisible();
});
