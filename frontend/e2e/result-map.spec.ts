/// <reference lib="dom" />
import { expect, test, type Page } from "@playwright/test";

const data = {
  type: "FeatureCollection", metadata: { renderer: "maplibre", indicator: "heat", indicator_label: "Heat", value_range: [25, 40], bounds: [[-117.4, 33.96], [-117.35, 34]] },
  features: [
    { type: "Feature", geometry: { type: "Polygon", coordinates: [[[-117.39, 33.97], [-117.36, 33.97], [-117.36, 33.99], [-117.39, 33.99], [-117.39, 33.97]]] }, properties: { layer_kind: "tract", GEOID: "test", indicator_value: 30 } },
    { type: "Feature", geometry: { type: "Point", coordinates: [-117.375, 33.98] }, properties: { layer_kind: "selected_site", name: "Selected library", candidate_id: "library-1" } },
  ],
};

async function openHarness(page: Page, key = "", estimate = false) {
  await page.route("**/api/v1/config", route => route.fulfill({ json: { carto_basemap_api_key: key } }));
  await page.route("**/result-map-test*", route => route.fulfill({ contentType: "text/html", body: '<html><body><div id="root"></div><script type="module">import RefreshRuntime from "/@react-refresh"; RefreshRuntime.injectIntoGlobalHook(window); window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => (type) => type; window.__vite_plugin_react_preamble_installed__ = true;</script><script type="module" src="/e2e/result-map-harness.tsx"></script></body></html>' }));
  await page.goto(`/result-map-test${estimate ? "?estimate=1" : ""}`);
}

async function expectFullWidth(page: Page) {
  await expect.poll(async () => page.locator(".result-map-panel").evaluate(element => {
    const content = element.parentElement!.getBoundingClientRect();
    const map = element.querySelector(".map-view")!.getBoundingClientRect();
    return Math.abs(content.width - map.width);
  })).toBeLessThan(3);
  await expect(page.locator(".map-canvas canvas")).toBeVisible();
  await expect.poll(async () => {
    const canvas = await page.locator(".map-canvas canvas").boundingBox();
    const view = await page.locator(".map-view").boundingBox();
    return Math.max(Math.abs(canvas!.width - view!.width), Math.abs(canvas!.height - view!.height));
  }).toBeLessThan(3);
}

test("ORS fallback keeps the estimated answer and interactive arrangement visible", async ({ page }) => {
  await page.route("**/test-map.geojson", route => route.fulfill({ json: data }));
  await page.setViewportSize({ width: 1400, height: 1050 });
  await openHarness(page, "", true);
  await expect(page.getByText("Estimated minimum: 10 facilities within 45 minutes.")).toBeVisible();
  await expect(page.getByLabel("Current decision context")).toContainText("Facilities10");
  const details = page.locator("details").filter({ has: page.locator("summary", { hasText: "ORS refinement unavailable" }) });
  await expect(details).not.toHaveAttribute("open");
  await expect(page.getByRole("button", { name: "Plan from map point", exact: true })).toBeEnabled();
  await expectFullWidth(page);
  await page.screenshot({ path: test.info().outputPath("estimate-with-map.png"), fullPage: true });
  await details.locator("summary").click();
  await expect(page.getByText("ORS request budget exceeded", { exact: true })).toBeVisible();
});

test("result maps use the full panel width, survive tab changes, and resize", async ({ page }) => {
  await page.route("**/test-map.geojson", route => route.fulfill({ json: data }));
  await page.setViewportSize({ width: 1400, height: 950 });
  await openHarness(page);
  await expect(page.getByRole("button", { name: "Plan from map point", exact: true })).toBeEnabled();
  await expectFullWidth(page);
  await page.screenshot({ path: test.info().outputPath("result-map-full-width.png") });
  await page.getByRole("tab", { name: "analysis", exact: true }).click();
  await page.getByRole("tab", { name: "map", exact: true }).click();
  await expect(page.getByRole("button", { name: "Plan from map point", exact: true })).toBeEnabled();
  await expectFullWidth(page);
  await page.getByRole("button", { name: "Show ordinary analysis" }).click();
  await expect(page.getByLabel("Coverage plan")).toHaveCount(0);
  await expectFullWidth(page);
  await page.setViewportSize({ width: 760, height: 950 });
  await expectFullWidth(page);
});

test("slow CARTO tiles do not block result layers or the route planner", async ({ page }) => {
  let releaseTiles: () => void = () => {};
  const pending = new Promise<void>(resolve => { releaseTiles = resolve; });
  let tileRequests = 0;
  await page.route("**/basemaps.cartocdn.com/**", async route => {
    tileRequests += 1;
    await pending;
    await route.abort().catch(() => {});
  });
  await page.route("**/test-map.geojson", route => route.fulfill({ json: data }));
  try {
    await openHarness(page, "test-key");
    await expect.poll(() => tileRequests).toBeGreaterThan(0);
    await expect(page.getByRole("button", { name: "Plan from map point", exact: true })).toBeEnabled();
    await expect(page.getByRole("status", { name: "" }).filter({ hasText: "Loading interactive map" })).toHaveCount(0);
    const canvas = await page.locator(".map-canvas canvas").boundingBox();
    await page.locator(".map-canvas canvas").click({ position: { x: canvas!.width / 2, y: canvas!.height / 2 } });
    await expect(page.locator(".map-popup")).toBeVisible();
    await expectFullWidth(page);
  } finally {
    releaseTiles();
  }
});

test("failed map downloads offer a retry without restarting the run", async ({ page }) => {
  let requests = 0;
  await page.route("**/test-map.geojson", route => {
    requests += 1;
    return requests === 1 ? route.fulfill({ status: 503, body: "Unavailable" }) : route.fulfill({ json: data });
  });
  await openHarness(page);
  await expect(page.getByRole("alert")).toContainText("Map data request failed (503)");
  await page.getByRole("button", { name: "Retry map" }).click();
  await expect(page.getByRole("button", { name: "Plan from map point", exact: true })).toBeEnabled();
  await expectFullWidth(page);
  expect(requests).toBe(2);
});
