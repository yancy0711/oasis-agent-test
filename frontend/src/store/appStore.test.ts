import { beforeEach, describe, expect, it } from "vitest";
import { useAppStore } from "./appStore";

describe("appStore", () => {
  beforeEach(() => {
    sessionStorage.clear();
    useAppStore.getState().resetSession();
  });

  it("registers completed runs idempotently", () => {
    useAppStore.getState().registerCompletedRun("run-1");
    useAppStore.getState().registerCompletedRun("run-1");
    expect(useAppStore.getState().completedRunIds).toEqual(["run-1"]);
  });

  it("stores only two unique comparison ids", () => {
    useAppStore.getState().setComparisonRunIds(["run-1", "run-1", "run-2", "run-3"]);
    expect(useAppStore.getState().comparisonRunIds).toEqual(["run-2", "run-3"]);
  });

  it("does not mix mobility runs from different scenario signatures", () => {
    useAppStore.getState().registerMobilityRun("scenario-a", "walking", "walk-1");
    useAppStore.getState().registerMobilityRun("scenario-a", "driving", "drive-1");
    expect(useAppStore.getState().mobilityRunIds).toEqual({ walking: "walk-1", driving: "drive-1" });
    useAppStore.getState().registerMobilityRun("scenario-b", "cycling", "cycle-2");
    expect(useAppStore.getState().mobilityRunIds).toEqual({ cycling: "cycle-2" });
  });
});
