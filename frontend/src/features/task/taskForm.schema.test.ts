import { describe, expect, it } from "vitest";
import { taskFormSchema } from "./taskForm.schema";
import { taskFromDraft, toTaskFormValues } from "./taskForm.mapper";

describe("advanced task form validation", () => {
  it("rejects reversed service hours", () => {
    const values = { ...toTaskFormValues(taskFromDraft({})), operating_window_enabled: true, service_start: "18:00", service_end: "09:00" };
    expect(taskFormSchema.safeParse(values).success).toBe(false);
  });
  it("rejects sensitivity grids above 36 experiments", () => {
    const values = { ...toTaskFormValues(taskFromDraft({ analysis_goal: "sensitivity_analysis" })), sensitivity_thresholds: [1,2,3,4,5,6,7], sensitivity_facility_counts: [1,2,3,4,5,6] };
    expect(taskFormSchema.safeParse(values).success).toBe(false);
  });
  it("accepts regional minimums and a valid sensitivity grid", () => {
    const values = { ...toTaskFormValues(taskFromDraft({ analysis_goal: "sensitivity_analysis" })), sensitivity_thresholds: [10,15], sensitivity_facility_counts: [4,8], regional_requirements: [{ region: "District 1", minimum_facilities: 1 }] };
    expect(taskFormSchema.safeParse(values).success).toBe(true);
  });
  it("rejects a Pareto alpha combined with a hard coverage target", () => {
    const values = { ...toTaskFormValues(taskFromDraft({})), equity_alpha_enabled: true,
      equity_alpha: .5, coverage_target_enabled: true, coverage_target: .8 };
    expect(taskFormSchema.safeParse(values).success).toBe(false);
  });
});
