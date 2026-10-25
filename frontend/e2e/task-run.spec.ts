import { expect, test } from "@playwright/test";
import type {
  ParseResponse, PlaceConfirmResponse, PlaceResolveResponse, RunAccepted,
  RunRequest, RunSnapshot, TaskSpec, UiConfigResponse,
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

// This test exercises browser/API contracts without LLM or geographic services.
test("parse, confirm, start, poll, cancel, and reload preserve one run", async ({ page }) => {
  let status: RunSnapshot["status"] = "running";
  let confirmedTask = task;
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
      preview_result: null, result: null, error: null, cancel_reason: null,
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
        return json(config);
      case "POST /api/v1/llm/models":
        return json({ provider: "openrouter", models: ["openai/gpt-4o"] });
      case "POST /api/v1/requests/interpret":
        return json({ action: "select_facilities", workflow: "research", reason: "Facility portfolio analysis", preferences: null });
      case "POST /api/v1/parse":
        return json({
          disposition: "full_analysis", task, missing_place: false, message: "",
          reason: "Supported candidate-facility analysis.", alternative: "",
          events: [], route_origin_candidates: [], place_name_options: [],
        } satisfies ParseResponse);
      case "POST /api/v1/places/resolve":
        return json({
          resolution_id: "resolution-1", query: task.place_name,
          ambiguous: false, candidates: [place], selected: null,
        } satisfies PlaceResolveResponse);
      case "POST /api/v1/places/confirm":
        return json(place);
      case "POST /api/v1/runs":
        confirmedTask = (request.postDataJSON() as RunRequest).task;
        return json({ run_id: "run-1", status: "queued", mode: "auto" } satisfies RunAccepted, 202);
      case "GET /api/v1/runs/run-1":
        return json(snapshot());
      case "POST /api/v1/runs/run-1/cancel":
        status = "cancelled";
        return json(snapshot(), 202);
      default:
        unexpectedRequests.push(endpoint);
        return json({ detail: `Unmocked endpoint: ${endpoint}` }, 501);
    }
  });

  await page.goto("/");
  await page.getByRole("tab", { name: "Find a nearby facility" }).click();
  await page.getByRole("checkbox", { name: "Auto-detect task type" }).check();
  await page.getByRole("textbox", { name: "Describe the cooling-access question", exact: true })
    .fill("Select 8 candidate cooling facilities in Phoenix, Arizona within a 15-minute walk.");
  await page.getByRole("button", { name: "Analyze request" }).click();
  await expect(page.getByRole("tab", { name: "Research and planning" })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("heading", { name: "Confirm the boundary" })).toBeVisible();
  const runButton = page.getByRole("button", { name: "Run confirmed agent" });
  await expect(runButton).toBeDisabled();
  await page.getByRole("button", { name: /Phoenix, Arizona, USA/ }).click();
  await expect(page.getByRole("spinbutton", { name: "Facilities", exact: true })).toHaveValue("8");
  await expect(page.getByRole("combobox", { name: "Mobility", exact: true })).toHaveValue("walking");
  await expect(runButton).toBeEnabled();

  const startRequest = page.waitForRequest((request) =>
    request.method() === "POST" && new URL(request.url()).pathname === "/api/v1/runs");
  await runButton.click();
  expect((await startRequest).postDataJSON()).toMatchObject({
    mode: "auto", task: {
      place_name: place.canonical_name, n_facilities: 8,
      threshold_minutes: 15, mobility_scenario: "walking",
    },
  });
  await expect(page.getByText("Preparing demand", { exact: true })).toBeVisible();
  await expect(page.getByText("Resolve the confirmed area.", { exact: true })).toBeVisible();
  await expect(page.locator(".run-summary .status")).toHaveText("running");

  await page.getByRole("button", { name: "Cancel run" }).click();
  await expect(page.locator(".run-summary .status")).toHaveText("cancelled");
  await expect(page.getByRole("button", { name: "Cancel run" })).toHaveCount(0);
  await expect(page.getByText("Preparing demand", { exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.locator(".run-summary .status")).toHaveText("cancelled");
  await expect(page.getByRole("button", { name: "Cancel run" })).toHaveCount(0);
  expect(unexpectedRequests).toEqual([]);
  expect(pageErrors).toEqual([]);
});
