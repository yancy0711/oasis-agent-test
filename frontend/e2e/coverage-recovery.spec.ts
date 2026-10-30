import { expect, test, type Page } from "@playwright/test";

async function openHarness(page: Page) {
  await page.route("**/coverage-recovery-test", route => route.fulfill({ contentType: "text/html", body: '<html><body><div id="root"></div><script type="module">import RefreshRuntime from "/@react-refresh"; RefreshRuntime.injectIntoGlobalHook(window); window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => (type) => type; window.__vite_plugin_react_preamble_installed__ = true;</script><script type="module" src="/e2e/coverage-recovery-harness.tsx"></script></body></html>' }));
  await page.goto("/coverage-recovery-test");
}

test("recommended repair changes one variable and keeps the exact coverage requirement", async ({ page }) => {
  await openHarness(page);
  await expect(page.getByText("Keep 12 minutes: use 15 facilities.")).toBeVisible();
  await expect(page.getByText("Keep 10 facilities: use 102.7 minutes.")).toBeVisible();
  await page.getByRole("button", { name: "Apply to task editor" }).click();
  await page.getByRole("button", { name: "Run confirmed agent" }).click();
  const output = page.getByTestId("submitted-task");
  await expect(output).toContainText('"n_facilities":15');
  await expect(output).toContainText('"threshold_minutes":12');
  await expect(output).toContainText('"coverage_target":0.99');
});

test("rounded time advice and a manual 148-minute value are valid form inputs", async ({ page }) => {
  await openHarness(page);
  await page.getByRole("button", { name: "Use travel-time option" }).click();
  await page.getByRole("button", { name: "Run confirmed agent" }).click();
  await expect(page.getByTestId("submitted-task")).toContainText('"threshold_minutes":102.7');
  await expect(page.getByTestId("submitted-task")).toContainText('"coverage_target":0.99');
  await page.getByRole("button", { name: "Threshold (minutes)", exact: true }).click();
  await page.getByLabel("Custom Threshold (minutes)", { exact: true }).fill("148");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Run confirmed agent" }).click();
  await expect(page.getByTestId("submitted-task")).toContainText('"threshold_minutes":148');
  await expect(page.getByTestId("submitted-task")).toContainText('"n_facilities":10');
  await expect(page.getByTestId("submitted-task")).toContainText('"coverage_target":0.99');
});

test("the extension replaces operating-window controls without silently changing the budget", async ({ page }) => {
  await openHarness(page);
  await expect(page.getByText("Operating window", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Filter candidate availability by local service hours", { exact: true })).toHaveCount(0);
  const extension = page.getByRole("checkbox", { name: "Allow hypothetical sites (extension)", exact: true });
  await expect(extension).toBeVisible();
  await expect(extension).toBeEnabled();
  await extension.check();
  await page.getByRole("button", { name: "Run confirmed agent" }).click();
  await expect(page.getByTestId("submitted-task")).toContainText('"allow_hypothetical_sites":true');
  await expect(page.getByTestId("submitted-task")).toContainText('"analysis_goal":"facility_siting"');
  await expect(page.getByTestId("submitted-task")).toContainText('"n_facilities":10');
  await expect(page.getByTestId("submitted-task")).toContainText('"threshold_minutes":12');
  await expect(page.getByTestId("submitted-task")).toContainText('"coverage_target":0.99');
  await expect(page.getByTestId("submitted-task")).toContainText('"operating_window":null');
});
