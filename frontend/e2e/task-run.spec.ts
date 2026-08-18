import { expect, test } from "@playwright/test";

const task = {
  place_name: "Phoenix",
  intervention: "cooling_center",
  analysis_goal: "facility_siting",
  candidate_source: "osm",
  candidate_file: null,
  mobility_scenario: "walking",
  recommended_mobility_scenario: null,
  recommended_value_profile: null,
  recommendation_rationale: "",
  operating_window: null,
  n_facilities: 8,
  threshold_minutes: 15,
  coverage_target: null,
  coverage_group: "all",
  weights: null,
  candidate_amenities: ["library", "community_centre"],
  regional_requirements: [],
  sensitivity_thresholds: [],
  sensitivity_facility_counts: [],
  recommended_sensitivity_thresholds: [],
  recommended_sensitivity_facility_counts: [],
  preference_question: "",
  notes: "",
};

test("parse, confirm, start, poll, and cancel share one run snapshot", async ({ page }) => {
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const json = (body: unknown, status = 200) => route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(body),
    });

    if (path.endsWith("/parse")) return json({ task, missing_place: false, message: "", events: [] });
    if (path.endsWith("/places/resolve")) return json({
      resolution_id: "resolution-1",
      query: "Phoenix",
      ambiguous: false,
      candidates: [{
        candidate_id: "candidate-1",
        canonical_name: "Phoenix, Arizona, USA",
        display_name: "Phoenix, Maricopa County, Arizona, USA",
        place_type: "city",
        label: "Phoenix (city)",
      }],
      selected: null,
    });
    if (path.endsWith("/places/confirm")) return json({
      candidate_id: "candidate-1",
      canonical_name: "Phoenix, Arizona, USA",
      display_name: "Phoenix, Maricopa County, Arizona, USA",
      place_type: "city",
      label: "Phoenix (city)",
      query: "Phoenix",
    });
    if (path.endsWith("/runs") && request.method() === "POST") {
      return json({ run_id: "run-1", status: "queued", mode: "auto" }, 202);
    }

    const snapshot = {
      run_id: "run-1",
      parent_run_id: null,
      revision: 0,
      status: path.endsWith("/cancel") ? "cancelled" : "running",
      phase: "quick",
      mode: "auto",
      spec: { task, mode: "auto" },
      task: { ...task, place_name: "Phoenix, Arizona, USA", weights: {
        population_density: 0.4,
        elderly_ratio: 0.2,
        poverty_rate: 0.2,
        heat_exposure: 0.2,
      } },
      events: [{ seq: 1, kind: "agent", stage: "decision", message: "Resolve the confirmed area.", tool: "resolve_study_area", data: {} }],
      progress: { kind: "progress", label: "Preparing demand", done: 1, total: 4, unit: "stages", fraction: 0.25, cache_hits: 0, remote_pairs: 0, inflight_pairs: 0 },
      preview_result: null,
      result: null,
      error: null,
      cancel_reason: null,
      created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:01Z",
    };
    return json(snapshot, path.endsWith("/cancel") ? 202 : 200);
  });

  await page.goto("/");
  await page.getByLabel("Describe the siting question").fill("Site cooling centers in Phoenix");
  await page.getByRole("button", { name: "Analyze request" }).click();
  await expect(page.getByRole("heading", { name: "Confirm the boundary" })).toBeVisible();
  await page.getByRole("button", { name: /Phoenix, Arizona, USA/ }).click();
  await expect(page.getByRole("heading", { name: "Review the executable task" })).toBeVisible();
  const mobilityBox = await page.getByRole("combobox", { name: /Mobility/ }).boundingBox();
  const executionBox = await page.getByRole("combobox", { name: "Execution" }).boundingBox();
  expect(mobilityBox?.y).toBeCloseTo(executionBox?.y ?? 0, 0);
  expect(mobilityBox?.height).toBeCloseTo(executionBox?.height ?? 0, 0);
  await page.getByRole("button", { name: "Run confirmed agent" }).click();
  await expect(page.getByText("Preparing demand")).toBeVisible();
  await expect(page.getByText("Resolve the confirmed area.")).toBeVisible();
  const traceBox = await page.locator(".trace-panel").boundingBox();
  const resultBox = await page.locator(".result-panel").boundingBox();
  expect(traceBox?.height).toBeCloseTo(resultBox?.height ?? 0, 0);
  await page.getByRole("button", { name: "Cancel run" }).click();
  await expect(page.getByText("cancelled", { exact: true })).toBeVisible();
});
