import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { RunProgress } from "./RunProgress";

describe("run progress", () => {
  it("keeps a solid full bar after a successful run", () => {
    render(<RunProgress status="succeeded" phase="precise" progress={null} />);
    expect(screen.getByText("Run complete")).toBeInTheDocument();
    expect(screen.getByText("100%")).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "100");
  });

  it("shows heartbeat timing while a stage is active", () => {
    render(<RunProgress status="running" phase="quick" progress={{
      kind: "progress", label: "Minimum-threshold search", done: 1, total: 10,
      unit: "probes", fraction: 0.1, cache_hits: 0, remote_pairs: 0,
      inflight_pairs: 0, stage_elapsed_seconds: 12, last_activity_seconds: 5,
    }} />);
    expect(screen.getByText("Stage elapsed 12s")).toBeInTheDocument();
    expect(screen.getByText("Last activity 5s ago")).toBeInTheDocument();
  });});
