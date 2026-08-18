import { describe, expect, it } from "vitest";
import type { RunSnapshot } from "../../api/models";
import { agentMessage, isTerminalRun, visibleResult } from "./run.selectors";

describe("run selectors", () => {
  it("uses the preview until the final result arrives", () => {
    const preview = { ok: true, message: "preview" };
    const snapshot = { status: "running", preview_result: preview } as unknown as RunSnapshot;
    expect(visibleResult(snapshot)).toBe(preview);
    expect(isTerminalRun(snapshot)).toBe(false);
  });

  it("derives recovery text without storing agentMessage", () => {
    const snapshot = {
      status: "awaiting_hitl",
      result: { ok: false, recovery: { diagnosis: "Infeasible", question: "Revise?" } },
    } as unknown as RunSnapshot;
    expect(agentMessage(snapshot)).toBe("Infeasible");
    expect(isTerminalRun(snapshot)).toBe(true);
  });
});
