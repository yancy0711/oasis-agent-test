import { expect, test } from "@playwright/test";

test("point planner cancels a pending request, retries and displays gateway failures", async ({ page }) => {
  await page.route("**/api/v1/config", route => route.fulfill({ json: { carto_basemap_api_key: "", agent: {}, execution: {}, optimization: {}, accessibility: {}, value_profiles: {} } }));
  await page.route("**/test-map.geojson", route => route.fulfill({ json: {
    type: "FeatureCollection", features: [], metadata: { renderer: "maplibre", indicator: "heat", indicator_label: "Heat", value_range: null, bounds: [[-117.4,33.96],[-117.35,34]] },
  } }));
  let release: () => void = () => {};
  let requests = 0;
  await page.route("**/api/v1/runs/route-test/routes**", async route => {
    requests += 1;
    expect(route.request().postDataJSON().mode).toBe("driving");
    if (requests === 1) await new Promise<void>(resolve => { release = resolve; });
    if (requests === 3) return route.fulfill({ status: 504, contentType: "text/html", body: "<html>Gateway timeout</html>" });
    await route.fulfill({ json: { type: "FeatureCollection", features: [], summary: { destination_id: "selected-1", destination_name: "Selected library", duration_minutes: 5, mode: "driving", origin_longitude: -117.37, origin_latitude: 33.98 } } }).catch(() => {});
  });
  await page.route("**/point-route-test", route => route.fulfill({ contentType: "text/html", body: '<html><body><div id="root"></div><script type="module">import RefreshRuntime from "/@react-refresh"; RefreshRuntime.injectIntoGlobalHook(window); window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => (type) => type; window.__vite_plugin_react_preamble_installed__ = true;</script><script type="module" src="/e2e/point-route-harness.tsx"></script></body></html>' }));
  await page.goto("/point-route-test");
  await expect(page.getByLabel("Route mode")).toHaveValue("driving");
  await page.getByRole("button", { name: "Plan from map point", exact: true }).click();
  await page.locator(".map-canvas canvas").click({ position: { x: 650, y: 400 } });
  await expect.poll(() => requests).toBe(1);
  await page.getByRole("button", { name: "Cancel route request" }).click();
  await expect(page.getByText("Route request cancelled. Choose another point or retry.")).toBeVisible();
  release();
  await page.getByLabel("Route origin address").fill("Riverside Convention Center");
  await page.getByRole("button", { name: "Plan", exact: true }).click();
  await expect(page.locator(".map-route-status")).toContainText("Selected library");
  await page.getByRole("button", { name: "Plan", exact: true }).click();
  await expect(page.locator(".map-route-status")).toContainText("HTTP 504");
  await expect(page.getByRole("button", { name: "Plan", exact: true })).toBeEnabled();
});
