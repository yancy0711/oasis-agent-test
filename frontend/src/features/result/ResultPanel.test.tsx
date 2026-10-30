import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { RunSnapshot } from "../../api/models";
import { TaskEditorProvider } from "../task/TaskEditorContext";
import { ResultPanel } from "./ResultPanel";

vi.mock("./MetricsView", () => ({ MetricsView: () => null }));
vi.mock("./ResultTabs", () => ({ ResultTabs: () => null }));
const retry = vi.hoisted(() => ({ mutate: vi.fn(), isPending: false, error: null }));
vi.mock("../run/run.mutations", () => ({ useReviseRun: () => retry }));
afterEach(cleanup);

it("keeps the computed count prominent and collapses ORS fallback diagnostics", () => {
  const snapshot = { run_id: "estimated-count", status: "succeeded", error: "threshold exceeded local policy", result: {
    task: { analysis_goal: "minimum_facilities", place_name: "Riverside", n_facilities: 10, threshold_minutes: 45, mobility_scenario: "walking", diagnostic_fixed_inputs: ["threshold_minutes"] },
    message: "Estimated minimum: 12 facilities within 45 minutes.",
    execution: { phase: "quick", status: "complete", accuracy: "approximate", router_used: "haversine", router_requested: "haversine" },
    diagnostics: { minimum_facilities: { feasible: true, minimum_facilities: 12 }, precise_refinement: { status: "unavailable", reason: "threshold exceeded local policy" } },
  } } as unknown as RunSnapshot;
  render(<TaskEditorProvider><ResultPanel snapshot={snapshot} /></TaskEditorProvider>);
  expect(screen.getByText("Estimated minimum: 12 facilities within 45 minutes.")).not.toHaveClass("agent-message--warning");
  expect(screen.getByLabelText("Current decision context")).toHaveTextContent("Facilities12");
  expect(screen.getByText("Approximate result")).toBeInTheDocument();
  const details = screen.getByText(/ORS refinement unavailable/).closest("details");
  expect(details).not.toHaveAttribute("open");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("restores an estimated answer from diagnostics in older saved warning-only runs", () => {
  const snapshot = { run_id: "old-estimate", status: "succeeded", error: "ORS unavailable", preview_result: {
    task: { analysis_goal: "minimum_facilities", place_name: "Riverside", n_facilities: 10, threshold_minutes: 45, mobility_scenario: "walking" },
    message: "The full study-area ORS matrix was unavailable, so the completed approximate selection result has been retained.",
    execution: { phase: "quick", status: "complete", accuracy: "approximate", router_used: "haversine", router_requested: "haversine" },
    diagnostics: { minimum_facilities: { feasible: true, minimum_proven: true, minimum_facilities: 12, threshold_minutes: 45 }, precise_refinement: { status: "unavailable", reason: "ORS unavailable" } },
  } } as unknown as RunSnapshot;
  render(<TaskEditorProvider><ResultPanel snapshot={snapshot} /></TaskEditorProvider>);
  expect(screen.getByText("Estimated minimum: 12 facilities within 45 minutes.")).toBeInTheDocument();
  expect(screen.queryByText(/The full study-area ORS matrix was unavailable/)).not.toBeInTheDocument();
});

it("shows provider recovery once, inside the alert", () => {
  const diagnosis = "OpenStreetMap place lookup failed. Your inputs are kept.";
  const snapshot = {
    run_id: "failed-osm", status: "awaiting_hitl",
    result: {
      task: { place_name: "Los Angeles", n_facilities: 10, threshold_minutes: 15, mobility_scenario: "walking" },
      execution: { phase: "quick", status: "failed", router_requested: "haversine", router_used: null },
      message: diagnosis,
      recovery: { diagnosis, question: "Retry the confirmed task later.", suggested_changes: {} },
    },
  } as unknown as RunSnapshot;
  render(<TaskEditorProvider><ResultPanel snapshot={snapshot} /></TaskEditorProvider>);
  expect(screen.getAllByText(diagnosis)).toHaveLength(1);
  expect(screen.getByRole("alert")).toHaveTextContent(diagnosis);
  expect(screen.getByRole("alert")).toHaveClass("error");
  fireEvent.click(screen.getByRole("button", { name: "Retry same task" }));
  expect(retry.mutate).toHaveBeenCalledWith({}); // Inherit the server's confirmed task and mode.
});

it("retains ordinary result messages without recovery", () => {
  const snapshot = {
    run_id: "completed", status: "succeeded",
    result: {
      task: { place_name: "Riverside", n_facilities: 10, threshold_minutes: 15, mobility_scenario: "walking" },
      execution: { phase: "quick", status: "complete", router_requested: "haversine", router_used: "haversine" },
      message: "The approximate selection is available.",
    },
  } as unknown as RunSnapshot;
  render(<TaskEditorProvider><ResultPanel snapshot={snapshot} /></TaskEditorProvider>);
  expect(screen.getByText("The approximate selection is available.")).toBeInTheDocument();
});
