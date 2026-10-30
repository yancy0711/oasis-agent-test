import type { TaskSpec } from "../../api/models";
import type { TaskFormValues } from "./taskForm.schema";

const DEFAULT_TASK: TaskSpec = {
  place_name: "", intervention: "cooling_center", analysis_goal: "facility_siting",
  place_names: [], result_queries: [], ambiguities: [],
  candidate_source: "osm", mobility_scenario: "walking", recommendation_rationale: "",
  n_facilities: 10, threshold_minutes: 15, facility_capacity: null, coverage_group: "all",
  candidate_amenities: ["library", "community_centre", "school", "sports_centre", "arts_centre", "place_of_worship"],
  regional_requirements: [], sensitivity_thresholds: [], sensitivity_facility_counts: [],
  comparison_profiles: [],
  recommended_sensitivity_thresholds: [], recommended_sensitivity_facility_counts: [],
  preference_question: "", notes: "",
};

export function taskFromDraft(draft: Record<string, unknown>): TaskSpec {
  return { ...DEFAULT_TASK, ...draft } as TaskSpec;
}

export function toTaskFormValues(task: TaskSpec): TaskFormValues {
  const window = task.operating_window;
  const diagnostic = ["minimum_facilities", "minimum_threshold"].includes(task.analysis_goal);
  return {
    analysis_goal: task.analysis_goal,
    allow_hypothetical_sites: task.allow_hypothetical_sites ?? false,
    n_facilities: task.n_facilities,
    threshold_minutes: task.threshold_minutes,
    diagnostic_fixed_inputs: task.diagnostic_fixed_inputs ?? (task.analysis_goal === "minimum_facilities"
      ? ["threshold_minutes"] : task.analysis_goal === "minimum_threshold" ? ["n_facilities"] : ["n_facilities", "threshold_minutes"]),
    facility_capacity_enabled: task.facility_capacity != null,
    facility_capacity: task.facility_capacity ?? 1000,
    equity_alpha_enabled: task.equity_alpha != null,
    equity_alpha: task.equity_alpha ?? 0.5,
    coverage_target_enabled: diagnostic || task.coverage_target != null,
    coverage_target: task.coverage_target ?? (diagnostic ? 1 : 0.8),
    mobility_scenario: task.mobility_scenario,
    candidate_source: task.candidate_source,
    coverage_group: task.coverage_group,
    candidate_amenities: task.candidate_amenities ?? [],
    comparison_profiles: task.comparison_profiles ?? [],
    weights: task.weights
      ? { ...task.weights, environment_burden: task.weights.environment_burden ?? 0 }
      : { population_density: 0.35, elderly_ratio: 0.15, poverty_rate: 0.15, heat_exposure: 0.2, environment_burden: 0.15 },
    operating_window_enabled: Boolean(window),
    service_date: window?.date ?? new Date().toISOString().slice(0, 10),
    service_start: window?.start?.slice(0, 5) ?? "09:00",
    service_end: window?.end?.slice(0, 5) ?? "17:00",
    unknown_policy: window?.unknown_policy ?? "include",
    sensitivity_thresholds: task.sensitivity_thresholds?.length ? task.sensitivity_thresholds : [task.threshold_minutes],
    sensitivity_facility_counts: task.sensitivity_facility_counts?.length ? task.sensitivity_facility_counts : [task.n_facilities],
    regional_requirements: task.regional_requirements ?? [],
  };
}

export function toTaskSpec(draft: TaskSpec, values: TaskFormValues, confirmedPlace: string): TaskSpec {
  let goal = values.analysis_goal as TaskSpec["analysis_goal"];
  if (values.coverage_target_enabled && values.diagnostic_fixed_inputs.length === 1) {
    goal = values.diagnostic_fixed_inputs.includes("threshold_minutes") ? "minimum_facilities" : "minimum_threshold";
  } else if (["minimum_facilities", "minimum_threshold"].includes(goal)) {
    goal = "facility_siting";
  }
  return {
    ...draft,
    analysis_goal: goal,
    allow_hypothetical_sites: goal === "minimum_facilities" && values.allow_hypothetical_sites,
    place_name: confirmedPlace,
    place_names: [confirmedPlace],
    ambiguities: [],
    n_facilities: values.n_facilities,
    threshold_minutes: values.threshold_minutes,
    diagnostic_fixed_inputs: ["minimum_facilities", "minimum_threshold"].includes(goal)
      ? values.diagnostic_fixed_inputs : null,
    facility_capacity: values.facility_capacity_enabled ? values.facility_capacity : null,
    equity_alpha: values.equity_alpha_enabled ? values.equity_alpha : null,
    coverage_target: values.coverage_target_enabled ? values.coverage_target : null,
    mobility_scenario: values.mobility_scenario,
    candidate_source: values.candidate_source,
    candidate_file: null,
    coverage_group: values.coverage_group,
    candidate_amenities: values.candidate_amenities,
    comparison_profiles: values.comparison_profiles,
    weights: draft.analysis_goal === "heat_review" ? null : values.weights,
    operating_window: values.operating_window_enabled ? {
      date: values.service_date, start: values.service_start, end: values.service_end, unknown_policy: values.unknown_policy,
    } : null,
    sensitivity_thresholds: values.sensitivity_thresholds,
    sensitivity_facility_counts: values.sensitivity_facility_counts,
    regional_requirements: values.regional_requirements,
    notes: `${draft.notes} [React HITL confirmed]`.trim(),
  };
}
