import type { TaskEditorPatch } from "../task/TaskEditorContext";

export function safeRecoveryPatch(changes: Record<string, unknown>): TaskEditorPatch {
  const patch: TaskEditorPatch = {};
  if (typeof changes.n_facilities === "number" && Number.isInteger(changes.n_facilities) && changes.n_facilities >= 1 && changes.n_facilities <= 100) patch.n_facilities = changes.n_facilities;
  if (typeof changes.threshold_minutes === "number" && Number.isFinite(changes.threshold_minutes) && changes.threshold_minutes > 0) patch.threshold_minutes = changes.threshold_minutes;
  if (Array.isArray(changes.candidate_amenities) && changes.candidate_amenities.every((item) => typeof item === "string")) patch.candidate_amenities = changes.candidate_amenities;
  return patch;
}
