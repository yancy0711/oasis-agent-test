import { describe, expect, it } from "vitest";
import { safeRecoveryPatch } from "./recovery.utils";

describe("recovery patch allowlist", () => {
  it("maps supported changes and ignores arbitrary model output", () => {
    expect(safeRecoveryPatch({
      n_facilities: 12, threshold_minutes: 20, coverage_target: .85,
      candidate_amenities: ["library", "school"], place_name: "Do not overwrite", weights: { poverty_rate: 1 },
    })).toEqual({
      n_facilities: 12, threshold_minutes: 20, coverage_target: .85,
      coverage_target_enabled: true, candidate_amenities: ["library", "school"],
    });
  });
});
