import { z } from "zod";

const weightsSchema = z.object({
  population_density: z.number().min(0).max(1),
  elderly_ratio: z.number().min(0).max(1),
  poverty_rate: z.number().min(0).max(1),
  heat_exposure: z.number().min(0).max(1),
  environment_burden: z.number().min(0).max(1),
}).refine(
  (weights) => Math.abs(Object.values(weights).reduce((sum, value) => sum + value, 0) - 1) < 0.0001,
  { message: "The five weights must sum to 1.00." },
);

export const taskFormSchema = z.object({
  analysis_goal: z.string(),
  n_facilities: z.number().int().min(1).max(100),
  threshold_minutes: z.number().positive(),
  facility_capacity_enabled: z.boolean(),
  facility_capacity: z.number().positive(),
  equity_alpha_enabled: z.boolean(),
  equity_alpha: z.number().min(0).max(1),
  coverage_target_enabled: z.boolean(),
  coverage_target: z.number().min(0).max(1),
  mobility_scenario: z.enum(["walking", "cycling", "driving", "walk_drive_equity"]),
  candidate_source: z.enum(["osm", "csv"]),
  coverage_group: z.enum(["all", "vulnerable"]),
  candidate_amenities: z.array(z.string()).min(1, "Select at least one OpenStreetMap facility type."),
  comparison_profiles: z.array(z.string()).max(6),
  weights: weightsSchema,
  operating_window_enabled: z.boolean(),
  service_date: z.string(),
  service_start: z.string(),
  service_end: z.string(),
  unknown_policy: z.enum(["include", "exclude"]),
  sensitivity_thresholds: z.array(z.number().positive()).max(12),
  sensitivity_facility_counts: z.array(z.number().int().min(1).max(100)).max(12),
  regional_requirements: z.array(z.object({
    region: z.string().min(1, "Enter a region name."),
    minimum_facilities: z.number().int().min(1).max(100),
  })),
}).superRefine((values, context) => {
  if (values.operating_window_enabled) {
    if (!values.service_date || !values.service_start || !values.service_end) {
      context.addIssue({ code: "custom", path: ["service_date"], message: "Complete the date and service hours." });
    } else if (values.service_end <= values.service_start) {
      context.addIssue({ code: "custom", path: ["service_end"], message: "End time must be after start time." });
    }
  }
  if (values.analysis_goal === "sensitivity_analysis") {
    if (!values.sensitivity_thresholds.length || !values.sensitivity_facility_counts.length) {
      context.addIssue({ code: "custom", path: ["sensitivity_thresholds"], message: "Add at least one threshold and facility count." });
    } else if (values.sensitivity_thresholds.length * values.sensitivity_facility_counts.length > 36) {
      context.addIssue({ code: "custom", path: ["sensitivity_thresholds"], message: "The sensitivity grid is limited to 36 experiments." });
    }
  }
  if (values.analysis_goal === "scenario_comparison" && values.comparison_profiles.length < 2) {
    context.addIssue({ code: "custom", path: ["comparison_profiles"], message: "Select at least two value profiles." });
  }
  if (values.equity_alpha_enabled && values.coverage_target_enabled) {
    context.addIssue({ code: "custom", path: ["equity_alpha"], message: "Choose either a Pareto alpha or a hard coverage target, not both." });
  }
});

export type TaskFormValues = z.infer<typeof taskFormSchema>;
