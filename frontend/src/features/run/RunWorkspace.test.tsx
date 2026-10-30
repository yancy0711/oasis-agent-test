import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { RunWorkspace } from "./RunWorkspace";

const retry = vi.hoisted(() => vi.fn());
vi.mock("../../store/appStore", () => ({ useAppStore: () => "saved-run" }));
vi.mock("./useRunLifecycle", () => ({ useRunLifecycle: () => undefined }));
vi.mock("./useRunPolling", () => ({ useRunPolling: () => ({
  isError: true, isLoading: false, error: new Error("HTTP 502"), refetch: retry,
  data: { run_id: "saved-run", status: "running", task: { place_name: "Riverside" } },
}) }));
vi.mock("../result/ResultPanel", () => ({ ResultPanel: () => <div>Saved result</div> }));
vi.mock("./AgentTrace", () => ({ AgentTrace: () => null }));
vi.mock("./RunControls", () => ({ RunControls: () => null }));
vi.mock("./RunProgress", () => ({ RunProgress: () => null }));
afterEach(cleanup);

it("retains the last snapshot during a proxy outage and offers a read-only retry", () => {
  render(<RunWorkspace />);
  expect(screen.getByText("Saved result")).toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent("current state is unknown");
  fireEvent.click(screen.getByRole("button", { name: "Retry connection" }));
  expect(retry).toHaveBeenCalledOnce();
});
