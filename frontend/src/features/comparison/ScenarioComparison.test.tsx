import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../../store/appStore";
import { ScenarioComparison } from "./ScenarioComparison";

vi.mock("./comparison.queries", () => ({
  useComparisonRuns: (runIds: string[]) => runIds.map((runId, index) => ({
    isLoading: false, isError: false,
    data: {
      run_id: runId, status: "succeeded", task: {
        place_name: "Phoenix", intervention: "cooling_center", analysis_goal: "facility_siting",
        candidate_source: "osm", mobility_scenario: "walking", n_facilities: 8,
        threshold_minutes: 15, coverage_group: "all", candidate_amenities: [], regional_requirements: [],
        sensitivity_thresholds: [], sensitivity_facility_counts: [], recommended_sensitivity_thresholds: [],
        recommended_sensitivity_facility_counts: [], preference_question: "", recommendation_rationale: "", notes: "",
        weights: index === 0
          ? { population_density: .4, elderly_ratio: .2, poverty_rate: .2, heat_exposure: .2, environment_burden: 0 }
          : { population_density: .1, elderly_ratio: .3, poverty_rate: .3, heat_exposure: .3, environment_burden: 0 },
      },
      result: { ok: true, paused: false, message: "done", metrics: { coverage: index ? .82 : .75 },
        execution: { mode: "precise", phase: "precise", router_requested: "ors", accuracy: "network", status: "complete" },
        results: [{ tool: "optimize_sites", ok: true, summary: { selected_ids: index ? ["b", "c"] : ["a", "b"] } }], diagnostics: {} },
    },
  })),
}));

beforeEach(() => useAppStore.setState({ completedRunIds: ["run-a"], comparisonRunIds: [] }));
afterEach(cleanup);

describe("scenario comparison completion handshake", () => {
  it("selects and loads both runs when the second run completes after mount", async () => {
    render(<ScenarioComparison />);
    expect(screen.getByText("Complete two successful runs to compare scenarios.")).toBeInTheDocument();
    act(() => useAppStore.getState().registerCompletedRun("run-b"));
    await waitFor(() => expect(screen.getAllByText("Phoenix")).toHaveLength(2));
    expect(screen.getByRole("heading", { name: "Alternative − baseline" })).toBeInTheDocument();
    expect(screen.getByText(/population density 0.40/)).toBeInTheDocument();
    expect(screen.getByText(/population density 0.10/)).toBeInTheDocument();
  });
});
