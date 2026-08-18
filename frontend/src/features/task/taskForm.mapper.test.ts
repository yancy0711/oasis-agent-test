import { describe, expect, it } from "vitest";
import { taskFromDraft, toTaskFormValues, toTaskSpec } from "./taskForm.mapper";

describe("task form mapper", () => {
  it("fills deterministic UI defaults and binds the confirmed place", () => {
    const draft = taskFromDraft({ place_name: "Phoenix", n_facilities: 4 });
    const values = toTaskFormValues(draft);
    const task = toTaskSpec(draft, values, "Phoenix, Arizona, USA");
    expect(task.place_name).toBe("Phoenix, Arizona, USA");
    expect(task.n_facilities).toBe(4);
    expect(Object.values(task.weights ?? {}).reduce((sum, value) => sum + value, 0)).toBe(1);
  });

  it("maps advanced confirmed controls without dropping parsed recommendations", () => {
    const draft = taskFromDraft({
      place_name: "Phoenix", analysis_goal: "sensitivity_analysis",
      recommended_sensitivity_thresholds: [10, 20], recommended_value_profile: "equity_first",
    });
    const values = {
      ...toTaskFormValues(draft), coverage_target_enabled: true, coverage_target: 0.9,
      facility_capacity_enabled: true, facility_capacity: 750,
      operating_window_enabled: true, service_date: "2026-08-05", service_start: "09:00", service_end: "17:00",
      sensitivity_thresholds: [10, 15, 20], sensitivity_facility_counts: [4, 8],
      regional_requirements: [{ region: "South Phoenix", minimum_facilities: 2 }],
    };
    const task = toTaskSpec(draft, values, "Phoenix, Arizona, USA");
    expect(task.operating_window).toEqual({ date: "2026-08-05", start: "09:00", end: "17:00", unknown_policy: "include" });
    expect(task.regional_requirements).toEqual([{ region: "South Phoenix", minimum_facilities: 2 }]);
    expect(task.coverage_target).toBe(.9);
    expect(task.facility_capacity).toBe(750);
    expect(task.sensitivity_facility_counts).toEqual([4, 8]);
    expect(task.recommended_value_profile).toBe("equity_first");
  });

  it("maps a confirmed Pareto alpha into the task", () => {
    const draft = taskFromDraft({ place_name: "Phoenix" });
    const values = { ...toTaskFormValues(draft), equity_alpha_enabled: true, equity_alpha: .7 };
    expect(toTaskSpec(draft, values, "Phoenix").equity_alpha).toBe(.7);
  });
});
