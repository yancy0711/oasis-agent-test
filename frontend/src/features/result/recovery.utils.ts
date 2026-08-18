import type { TaskEditorPatch } from "../task/TaskEditorContext";

export function safeRecoveryPatch(changes: Record<string, unknown>): TaskEditorPatch {
  const patch: TaskEditorPatch = {};
  if (typeof changes.n_facilities === "number") patch.n_facilities = changes.n_facilities;
  if (typeof changes.threshold_minutes === "number") patch.threshold_minutes = changes.threshold_minutes;
  if (typeof changes.coverage_target === "number") { patch.coverage_target = changes.coverage_target; patch.coverage_target_enabled = true; }
  if (Array.isArray(changes.candidate_amenities) && changes.candidate_amenities.every((item) => typeof item === "string")) patch.candidate_amenities = changes.candidate_amenities;
  return patch;
}
