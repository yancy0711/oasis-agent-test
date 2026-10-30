import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { RunResult } from "../../api/models";
import { ResultTabs } from "./ResultTabs";

vi.mock("./MapView", () => ({ MapView: ({ url, showRoutePlanner }: { url: string; showRoutePlanner: boolean }) =>
  <div data-testid="map">{url} {showRoutePlanner ? "routing enabled" : "routing hidden"}</div> }));
afterEach(cleanup);

it("explains why an infeasible extension has no map rather than appearing to keep loading", () => {
  const result = { task: { analysis_goal: "facility_siting", allow_hypothetical_sites: true },
    artifacts: {}, diagnostics: { hypothetical_coverage: { planning_mode: "fixed_budget", feasible: false } } } as unknown as RunResult;
  render(<ResultTabs runId="infeasible-extension" result={result} />);
  expect(screen.getByText(/no verified portfolio was produced/i)).toBeInTheDocument();
  expect(screen.queryByTestId("map")).not.toBeInTheDocument();
});

it("labels the hypothetical map and never offers navigation to synthetic locations", async () => {
  const result = { task: { analysis_goal: "minimum_facilities", mobility_scenario: "walking" },
    artifacts: { hypothetical_coverage_map: "/hypothetical.geojson" } } as unknown as RunResult;
  render(<ResultTabs runId="extension" result={result} />);
  expect(await screen.findByTestId("map")).toHaveTextContent("/hypothetical.geojson routing hidden");
  expect(screen.getByText(/Purple: hypothetical sites/)).toHaveTextContent("network coverage are unverified");
});

it("switches between the two diagnostic portfolios without changing the current run", async () => {
  const result = {
    task: { analysis_goal: "minimum_threshold", mobility_scenario: "walking" },
    artifacts: { map: "/threshold.geojson", minimum_facilities_map: "/facilities.geojson", minimum_threshold_map: "/threshold.geojson" },
  } as unknown as RunResult;
  render(<ResultTabs runId="paired" result={result} />);
  expect(await screen.findByTestId("map")).toHaveTextContent("/threshold.geojson routing enabled");
  fireEvent.change(screen.getByLabelText("Coverage plan"), { target: { value: "minimum_facilities_map" } });
  expect(await screen.findByTestId("map")).toHaveTextContent("/facilities.geojson routing hidden");
  fireEvent.change(screen.getByLabelText("Coverage plan"), { target: { value: "minimum_threshold_map" } });
  expect(await screen.findByTestId("map")).toHaveTextContent("/threshold.geojson routing enabled");
});
