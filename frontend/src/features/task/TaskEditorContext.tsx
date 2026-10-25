/* eslint-disable react-refresh/only-export-components */
import { createContext, useCallback, useContext, useMemo, useRef, type ReactNode } from "react";
import type { TaskFormValues } from "./taskForm.schema";

export type TaskEditorPatch = Partial<Pick<TaskFormValues,
  "n_facilities" | "threshold_minutes" | "coverage_target" | "coverage_target_enabled" |
  "candidate_source" | "candidate_amenities" | "mobility_scenario" | "sensitivity_thresholds" | "sensitivity_facility_counts" | "weights"
>>;

type Handler = (patch: TaskEditorPatch) => void;
const Context = createContext<{ apply: Handler; register: (handler: Handler | null) => void } | null>(null);

export function TaskEditorProvider({ children }: { children: ReactNode }) {
  const handler = useRef<Handler | null>(null);
  const register = useCallback((next: Handler | null) => { handler.current = next; }, []);
  const apply = useCallback<Handler>((patch) => handler.current?.(patch), []);
  const value = useMemo(() => ({ apply, register }), [apply, register]);
  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useTaskEditor() {
  const value = useContext(Context);
  if (!value) throw new Error("useTaskEditor must be used within TaskEditorProvider");
  return value;
}
