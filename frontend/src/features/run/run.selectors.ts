import type { RunResult, RunSnapshot } from "../../api/models";
import { TERMINAL_RUN_STATUSES } from "./run.constants";

export function isTerminalRun(snapshot: RunSnapshot | undefined): boolean {
  return Boolean(snapshot && TERMINAL_RUN_STATUSES.has(snapshot.status));
}

export function visibleResult(snapshot: RunSnapshot | undefined): RunResult | null {
  return snapshot?.result ?? snapshot?.preview_result ?? null;
}

export function agentMessage(snapshot: RunSnapshot | undefined): string | null {
  const result = visibleResult(snapshot);
  if (result?.recovery?.diagnosis) return result.recovery.diagnosis;
  if (result?.message) return result.message;
  return snapshot?.error ?? null;
}

export function isPreview(snapshot: RunSnapshot | undefined): boolean {
  return Boolean(snapshot?.preview_result && !snapshot.result);
}
