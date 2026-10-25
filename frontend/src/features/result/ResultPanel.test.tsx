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
