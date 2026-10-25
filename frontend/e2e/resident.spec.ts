import { expect, test, type Page } from "@playwright/test";
import type { Origin, Preferences, ResidentRoute, SearchResult } from "../src/features/resident/residentApi";
import { DEFAULT_PREFERENCES } from "../src/features/resident/residentApi";

const us: Origin = { longitude: -117.375, latitude: 33.981, label: "Riverside City Hall", country_code: "us" };
const abroad: Origin = { longitude: 114.17, latitude: 22.3, label: "Hong Kong", country_code: "hk" };
const prefs: Preferences = { ...DEFAULT_PREFERENCES, origin_query: null, use_current_location: false,
  threshold_minutes: 10, categories: ["mall"], excluded_categories: ["park"] };
const result: SearchResult = { search_id: "resident-search-1", origin: us, mode: "walking", threshold_minutes: 10,
  radius_km: 5, complete: true, candidate_limit: 50, discovered_count: 2, checked_count: 2, message: "Two matching malls within 10 minutes in the 5 km radius.", source: "OSM / ORS",
  candidates: [
    { id: "way/1", name: "Test Mall", category: "mall", longitude: -117.37, latitude: 33.98, duration_minutes: 4,
      opening_hours: "Unknown", access: "yes", fee: "no", indoor: "yes", coordinate_source: "Mapped entrance", osm_url: "https://www.openstreetmap.org/way/1" },
    { id: "way/2", name: "Second Mall", category: "mall", longitude: -117.38, latitude: 33.979, duration_minutes: 6,
      opening_hours: "Unknown", access: "Unknown", fee: "Unknown", indoor: "Unknown", coordinate_source: "Mapped center; entrance unverified", osm_url: "https://www.openstreetmap.org/way/2" },
  ] };

async function mockApi(page: Page, location: Origin, parsed: Preferences = prefs, failSearchOnce = false) {
  let searches = 0;
  const calls: { path: string; body: unknown }[] = [];
  const unexpected: string[] = [];
  await page.route((url) => url.pathname.startsWith("/api/"), async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const body = request.method() === "POST" ? request.postDataJSON() : null;
    calls.push({ path, body });
    const json = (payload: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(payload) });
    if (path === "/api/streetview") return json({ available: false, status: "no_image", provider: "Mapillary", message: "No nearby street imagery available." });
    if (path === "/api/v1/config") return json({ optimization: {}, accessibility: {}, agent: { provider: "openrouter", model: "test-model" },
      execution: {}, value_profiles: {}, carto_basemap_api_key: "" });
    if (path === "/api/v1/llm/models") return json({ provider: "openrouter", models: ["test-model"] });
    if (path === "/api/v1/requests/interpret") return json({ action: "find_destination", workflow: "personal", reason: "Nearby destinations", preferences: parsed });
    if (path === "/api/v1/resident/parse") return json(prefs);
    if (path === "/api/v1/resident/origin") return json(location);
    if (path === "/api/v1/resident/origins") return json({ candidates: [us], message: "Confirm your starting point." });
    if (path === "/api/v1/resident/search") {
      if (failSearchOnce && searches++ === 0) return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "OpenStreetMap place lookup failed. Select Retry search beside the map." }) });
      return json(result);
    }
    if (path.startsWith("/api/v1/resident/cancel/")) return json({ cancelled: true });
    if (path === "/api/v1/resident/routes") {
      const place = result.candidates.find((item) => item.id === body.candidate_id)!;
      const response: ResidentRoute = { type: "FeatureCollection", features: [{ type: "Feature", properties: {},
        geometry: { type: "LineString", coordinates: [[us.longitude, us.latitude], [place.longitude, place.latitude]] } }],
        summary: { destination_id: place.id, destination_name: place.name, duration_minutes: place.duration_minutes,
          distance_km: 0.5, green_score: place.id === "way/1" ? 2 : 8, within_threshold: true, heat_status: "unavailable", message: "Heat estimates unavailable." } };
      return json(response);
    }
    unexpected.push(path);
    return route.fulfill({ status: 500, body: "Unexpected request" });
  });
  return { calls, unexpected };
}

test("pending search animates and category select-all keeps the confirmed origin", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await mockApi(page, us);
  let release!: () => void;
  const waiting = new Promise<void>((resolve) => { release = resolve; });
  let submitted: { preferences: Preferences } | undefined;
  await page.route("**/api/v1/resident/search", async (route) => {
    submitted = route.request().postDataJSON();
    await waiting;
    await route.fulfill({ json: result });
  });
  await page.goto("/");
  await page.getByRole("tab", { name: "Find a nearby facility" }).click();
  await page.getByLabel("US address or landmark").fill("Riverside City Hall, California");
  await page.getByRole("button", { name: "Find address", exact: true }).click();
  await page.getByRole("checkbox", { name: "Select all place categories" }).check();
  await page.getByRole("button", { name: "Confirm location and search" }).click();
  const status = page.locator('.resident-request-status > p[role="status"]');
  await expect(status).toHaveAttribute("aria-busy", "true");
  await expect(status).toContainText("Finding nearby places");
  const dots = status.locator(".animated-ellipsis");
  await expect(dots).toHaveCount(1);
  expect(await page.evaluate('getComputedStyle(document.querySelector(".resident-request-status > p[role=status] .animated-ellipsis"), "::after").animationName')).toBe("ellipsis");
  await expect(page.getByRole("button", { name: "Select starting point on map" })).toBeEnabled();
  const tops: number[] = await page.evaluate('(async () => { const values = []; for (let i = 0; i < 14; i++) { values.push(document.querySelector(".resident-map").getBoundingClientRect().top); await new Promise(resolve => setTimeout(resolve, 100)); } return values; })()');
  expect(Math.max(...tops) - Math.min(...tops)).toBeLessThan(0.5);
  const confirmed = await page.locator(".resident-map-column > .notice").boundingBox();
  const stop = await page.getByRole("button", { name: "Stop and reset" }).boundingBox();
  const statusBox = await status.boundingBox();
  expect(stop!.y - (confirmed!.y + confirmed!.height)).toBeGreaterThanOrEqual(10);
  expect(statusBox!.y).toBeGreaterThan(stop!.y + stop!.height);
  await expect.poll(() => submitted?.preferences.categories?.length).toBe(8);
  expect(submitted?.preferences.excluded_categories).toEqual([]);
  release();
  await expect(page.getByText("Route to Test Mall", { exact: true })).toBeVisible();
  await expect(status).toHaveAttribute("aria-busy", "false");
  await expect(status.locator(".animated-ellipsis")).toHaveCount(0);
});

test("missing resident origin locates, warns outside US, then accepts a US address without solver", async ({ page, context }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await context.grantPermissions(["geolocation"]);
  await context.setGeolocation({ longitude: abroad.longitude, latitude: abroad.latitude });
  const { calls, unexpected } = await mockApi(page, abroad);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await page.getByRole("tab", { name: "Find a nearby facility" }).click();
  await page.getByRole("textbox", { name: "Describe the cooling-access question", exact: true }).fill("Find the nearest cooling center. I want a shopping mall, not a park.");
  await page.getByRole("button", { name: "Analyze request" }).click();
  await expect(page.getByText("Outside supported area", { exact: true })).toBeVisible();
  await expect(page.locator(".resident-origin--outside")).toBeVisible();
  await expect(page.getByRole("button", { name: "Update results", exact: true })).toHaveCount(0);
  expect(calls.some((call) => call.path === "/api/v1/resident/search")).toBe(false);
  await page.screenshot({ path: "../tmp/resident-outside.png", fullPage: true });

  await page.getByLabel("US address or landmark").fill("Riverside City Hall, California");
  await page.getByRole("button", { name: "Find address", exact: true }).click();
  await page.getByRole("button", { name: "Confirm location and search" }).click();
  await expect(page.getByRole("button", { name: "Route to Test Mall", exact: true })).toBeVisible();
  await expect(page.getByText("Route to Test Mall", { exact: true })).toBeVisible();
  await expect(page.getByRole("checkbox", { name: "Shopping malls", exact: true })).toBeChecked();
  await expect(page.getByRole("checkbox", { name: "Parks", exact: true })).not.toBeChecked();
  await expect(page.getByRole("spinbutton", { name: "Facilities", exact: true })).toHaveCount(0);
  expect(calls.find((call) => call.path === "/api/v1/resident/search")?.body).toMatchObject({
    longitude: us.longitude, latitude: us.latitude, preferences: { categories: ["mall"], excluded_categories: ["park"], threshold_minutes: 10 },
  });
  await expect(page.locator(".resident-route-legend")).toContainText("Greener compared route: Second Mall");
  await expect(page.locator(".resident-route-legend")).toContainText("Fastest: Test Mall");
  await expect(page.locator(".resident-route-swatch--fastest")).toHaveCSS("border-top-color", "rgb(198, 93, 19)");
  await expect(page.locator(".resident-fastest-route-label")).toHaveText("Fastest");
  await page.locator(".resident-explorer").screenshot({ path: "../tmp/resident-route-overview.png" });
  const originBox = (await page.locator(".resident-origin").boundingBox())!;
  const targetBox = (await page.getByRole("button", { name: "Directions to Test Mall on map", exact: true }).boundingBox())!;
  await page.mouse.click((originBox.x + originBox.width / 2 + targetBox.x + targetBox.width / 2) / 2,
    (originBox.y + originBox.height / 2 + targetBox.y + targetBox.height / 2) / 2);
  await expect(page.locator(".resident-route-popup")).toContainText("Test Mall");
  await expect(page.locator(".resident-route-popup")).toContainText("ORS green index: 2.0");
  const divider = page.getByRole("separator", { name: "Resize place list" });
  await divider.scrollIntoViewIfNeeded();
  const dividerBox = (await divider.boundingBox())!;
  await page.mouse.move(dividerBox.x + 5, dividerBox.y + 50);
  await page.mouse.down(); await page.mouse.move(dividerBox.x + 105, dividerBox.y + 50, { steps: 8 }); await page.mouse.up();
  expect(Number(await divider.getAttribute("aria-valuenow"))).toBeGreaterThan(25);
  await alignedPanels(page);
  await page.getByRole("button", { name: "Route to Second Mall", exact: true }).click();
  await expect(page.getByText("Route to Second Mall", { exact: true })).toBeVisible();
  await expect(page.locator(".resident-route-legend")).toHaveText("Selected route: Second Mall");
  await expect(page.locator(".resident-fastest-route-label")).toHaveCount(0);
  await expect(page.locator('[data-place-id="way/1"] .resident-fastest-badge')).toHaveText("Fastest");
  await expect(page.locator(".resident-place-popup")).toContainText("Second Mall");
  await expect(page.locator(".resident-place-popup")).toContainText("6.0 min walking");
  await expect(page.locator(".resident-place-popup")).not.toContainText("Opening hours");
  expect(calls.filter((call) => call.path === "/api/v1/resident/search")).toHaveLength(1);
  const sidebar = page.getByRole("complementary", { name: "Nearby candidate details" });
  await expect(sidebar.locator(".resident-place.is-selected")).toContainText("Second Mall");
  await page.getByRole("button", { name: "Directions to Test Mall on map", exact: true }).click();
  await expect(sidebar.locator(".resident-place.is-selected")).toContainText("Test Mall");
  await sidebar.getByRole("button", { name: "2. Second Mall", exact: true }).click();
  await expect(page.getByRole("button", { name: "Directions to Second Mall on map", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".resident-map-place")).toHaveCount(2);
  await alignedPanels(page);
  await page.screenshot({ path: "../tmp/resident-results.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: "../tmp/resident-mobile.png", fullPage: true });
  await expect.poll(() => page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")).toBe(true);
  await page.getByRole("tab", { name: "Research and planning" }).click();
  await expect(page.getByRole("button", { name: "Candidate facility siting", exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Resident nearby places" })).toHaveCount(0);
  expect(calls.some((call) => call.path === "/api/v1/runs" || call.path === "/api/v1/parse")).toBe(false);
  expect(unexpected).toEqual([]);
  expect(errors).toEqual([]);
});

test("Use my location bypasses LLM parsing and waits for confirmation", async ({ page, context }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await context.grantPermissions(["geolocation"]);
  await context.setGeolocation({ longitude: us.longitude, latitude: us.latitude });
  const { calls, unexpected } = await mockApi(page, us);
  await page.goto("/");
  await page.getByRole("tab", { name: "Find a nearby facility" }).click();
  await page.locator(".prompt-examples").getByRole("button", { name: "Use my location", exact: true }).click();
  await expect(page.getByRole("button", { name: "Confirm location and search" })).toBeVisible();
  expect(calls.some((call) => call.path.endsWith("/parse") || call.path.endsWith("/search"))).toBe(false);
  await page.getByRole("button", { name: "Confirm location and search" }).click();
  await expect(page.getByText("Route to Test Mall", { exact: true })).toBeVisible();
  expect(unexpected).toEqual([]);
});


test("a nearby request on the research tab resolves its address, permits retry and exposes map picking inside the map", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const parsed: Preferences = { ...DEFAULT_PREFERENCES, origin_query: "Riverside Convention Center",
    preferred_categories: ["mall"], notice: "Potential cooling places; opening hours are not verified.",
    clarification: "Cooling facilities are not guaranteed official cooling centers.", needs_clarification: false };
  const { calls, unexpected } = await mockApi(page, us, parsed, true);
  await page.goto("/");
  await page.getByRole("textbox", { name: "Describe the cooling-access question", exact: true }).fill("I'm at riverside conventional centre. How can I find the nearest cooling facility? I prefer shopping malls.");
  await page.getByRole("button", { name: "Analyze request" }).click();
  await expect(page.getByRole("tab", { name: "Find a nearby facility" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByLabel("US address or landmark")).toHaveValue("Riverside Convention Center");
  await page.getByRole("button", { name: "Confirm location and search" }).click();
  await expect(page.getByText("OpenStreetMap place lookup failed. Select Retry search beside the map.")).toBeVisible();
  const retry = page.getByRole("button", { name: "Retry search", exact: true });
  await expect(retry).toBeEnabled();
  await alignedPanels(page);
  await retry.click();
  await expect(page.getByText("Route to Test Mall", { exact: true })).toBeVisible();
  await page.locator(".resident-map-tools").getByRole("button", { name: "Select starting point on map" }).click();
  await page.locator(".resident-map canvas").click({ position: { x: 120, y: 120 } });
  await expect(page.getByRole("button", { name: "Confirm location and search" })).toBeVisible();
  const mapColumn = page.locator(".resident-map-column");
  await expect(mapColumn.getByRole("button", { name: "Confirm location and search" })).toBeVisible();
  await mapColumn.getByRole("button", { name: "Confirm location and search" }).click();
  await expect(page.getByText("Route to Test Mall", { exact: true })).toBeVisible();
  expect(calls.filter((call) => call.path === "/api/v1/resident/search")).toHaveLength(3);
  expect(calls.some((call) => call.path === "/api/v1/parse" || call.path === "/api/v1/resident/parse" || call.path === "/api/v1/runs")).toBe(false);
  expect(unexpected).toEqual([]);
});

async function alignedPanels(page: Page) {
  await expect.poll(() => page.evaluate(`(() => {
    const left = document.querySelector(".resident-candidate-sidebar").getBoundingClientRect();
    const right = document.querySelector(".resident-map").getBoundingClientRect();
    return Math.abs(left.bottom - right.bottom);
  })()`)).toBeLessThanOrEqual(1);
}


test("switching to nearby search waits for the current research run to stop", async ({ page }) => {
  const { unexpected } = await mockApi(page, us);
  const snapshot = { run_id: "active-test", status: "running", phase: "quick", mode: "quick",
    task: { place_name: "Phoenix, Arizona, USA" }, events: [], progress: null,
    result: null, preview_result: null, error: null };
  let stopped = false;
  let cancellationRequested = false;
  let acknowledge!: () => void;
  const acknowledgement = new Promise<void>((resolve) => { acknowledge = resolve; });
  await page.addInitScript(() => {
    sessionStorage.setItem("oasis-session", JSON.stringify({ state: { activeRunId: "active-test" }, version: 0 }));
  });
  await page.route("**/api/v1/runs/active-test", (route) => route.fulfill({ json: { ...snapshot, status: stopped ? "cancelled" : "running" } }));
  await page.route("**/api/v1/runs/active-test/cancel", async (route) => {
    cancellationRequested = true;
    await acknowledgement;
    stopped = true;
    await route.fulfill({ json: { ...snapshot, status: "cancelled" } });
  });
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Cancel run", exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "Find a nearby facility" }).click();
  await expect.poll(() => cancellationRequested).toBe(true);
  await expect(page.getByRole("tab", { name: "Research and planning" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("region", { name: "Resident nearby places" })).toHaveCount(0);
  acknowledge();
  await expect(page.getByRole("tab", { name: "Find a nearby facility" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("region", { name: "Resident nearby places" })).toBeVisible();
  expect(stopped).toBe(true);
  expect(unexpected).toEqual([]);
});

test("manual tab choice corrects an automatic workflow using the unchanged prompt", async ({ page }) => {
  const prompt = "Select 10 candidate cooling facilities near Raincross Hotel Riverside within a 15-minute walking threshold.";
  const { calls } = await mockApi(page, us, { ...prefs, origin_query: "Raincross Hotel Riverside" });
  let parses = 0;
  await page.route("**/api/v1/parse", async (route) => {
    expect(route.request().postDataJSON().prompt).toBe(prompt);
    parses++;
    await route.fulfill({ json: { disposition: "full_analysis", missing_place: false,
      reason: "Facility selection", alternative: "", events: [], route_origin_candidates: [], place_name_options: [],
      task: { place_name: "Riverside, California, USA", place_names: ["Riverside, California, USA"],
        analysis_goal: "facility_siting", n_facilities: 10, threshold_minutes: 15, route_origin: null },
    } });
  });
  await page.route("**/api/v1/places/resolve", (route) => route.fulfill({ json: {
    resolution_id: "riverside-boundary", query: "Riverside", ambiguous: false, selected: null,
    candidates: [{ candidate_id: "riverside-city", canonical_name: "Riverside, California, USA",
      display_name: "Riverside, California, USA", place_type: "city", label: "Riverside (city)", query: "Riverside" }],
  } }));
  await page.goto("/");
  const input = page.getByRole("textbox", { name: "Describe the cooling-access question", exact: true });
  await input.fill(prompt);
  await page.getByRole("button", { name: "Analyze request" }).click();
  await expect(page.getByRole("button", { name: "Confirm location and search" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Find a nearby facility" })).toHaveAttribute("aria-selected", "true");
  await page.getByRole("tab", { name: "Research and planning" }).click();
  await expect(page.getByRole("checkbox", { name: "Auto-detect task type" })).not.toBeChecked();
  await expect(input).toHaveValue(prompt);
  await page.getByRole("button", { name: "Analyze request" }).click();
  await expect(page.getByRole("heading", { name: "Confirm the boundary" })).toBeVisible();
  await expect(page.getByRole("spinbutton", { name: "Facilities", exact: true })).toHaveValue("10");
  expect(parses).toBe(1);
  expect(calls.filter((call) => call.path === "/api/v1/requests/interpret")).toHaveLength(1);
});
