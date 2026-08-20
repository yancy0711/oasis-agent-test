import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { TaskEditorProvider } from "../task/TaskEditorContext";
import { RecoveryPanel } from "./RecoveryPanel";
import { safeRecoveryPatch } from "./recovery.utils";

afterEach(cleanup);

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

  it("offers a study-area action for unsupported boundary recovery", () => {
    const result = {
      recovery: {
        diagnosis: "Unsupported study-area type census; use a city or county",
        question: "Please specify a valid city or county for the study area.",
        suggested_changes: {},
      },
    } as never;
    render(createElement(
      TaskEditorProvider,
      null,
      createElement(RecoveryPanel, { result }),
    ));
    expect(screen.getByRole("button", { name: "Choose a city or county" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Choose a city or county" }));
    expect(screen.queryByText("Human decision required")).not.toBeInTheDocument();
  });
});
