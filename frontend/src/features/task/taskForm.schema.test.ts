import { describe, expect, it } from "vitest";
import { taskFormSchema } from "./taskForm.schema";
import { taskFromDraft, toTaskFormValues, toTaskSpec } from "./taskForm.mapper";

describe("advanced task form validation", () => {
  it("does not submit a hidden operating-window filter after removing its controls", () => {
    const draft = taskFromDraft({ operating_window: { date: "2026-08-05", start: "09:00", end: "17:00", unknown_policy: "include" } });
    const values = toTaskFormValues(draft);
    expect(values).not.toHaveProperty("operating_window_enabled");
    expect(toTaskSpec(draft, values, "Riverside").operating_window).toBeNull();
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
