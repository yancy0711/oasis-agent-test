import type { RunSnapshot } from "../../api/models";

export type RunStatus = RunSnapshot["status"];

export const TERMINAL_RUN_STATUSES = new Set<RunStatus>([
  "cancelled",
  "superseded",
  "interrupted",
  "succeeded",
  "awaiting_hitl",
  "failed",
]);
