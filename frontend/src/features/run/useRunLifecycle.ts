import { useEffect } from "react";
import type { RunSnapshot } from "../../api/models";
import { useAppStore } from "../../store/appStore";

function mobilitySignature(snapshot: RunSnapshot): string {
  const task = snapshot.task;
  return JSON.stringify({
    place_name: task.place_name,
    intervention: task.intervention,
    analysis_goal: task.analysis_goal,
    candidate_source: task.candidate_source,
    n_facilities: task.n_facilities,
    threshold_minutes: task.threshold_minutes,
    coverage_target: task.coverage_target,
    coverage_group: task.coverage_group,
    weights: task.weights,
    candidate_amenities: task.candidate_amenities,
    operating_window: task.operating_window,
  });
}

export function useRunLifecycle(snapshot: RunSnapshot | undefined): void {
  const registerCompletedRun = useAppStore((state) => state.registerCompletedRun);
  const registerMobilityRun = useAppStore((state) => state.registerMobilityRun);

  useEffect(() => {
    if (!snapshot || snapshot.status !== "succeeded") return;
    registerCompletedRun(snapshot.run_id);
    registerMobilityRun(mobilitySignature(snapshot), snapshot.task.mobility_scenario, snapshot.run_id);
  }, [snapshot, registerCompletedRun, registerMobilityRun]);
}
