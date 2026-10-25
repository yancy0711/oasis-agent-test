import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createElement, useEffect } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TaskEditorProvider, useTaskEditor } from "../task/TaskEditorContext";
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

  it("offers actionable OSM recovery without silently switching the source", () => {
    const apply = vi.fn();
    const retry = vi.fn();
    function EditorListener() {
      const editor = useTaskEditor();
      useEffect(() => { editor.register(apply); return () => editor.register(null); }, [editor]);
      return null;
    }
    const result = { execution: { status: "failed" }, recovery: {
      diagnosis: "OpenStreetMap place lookup failed (ReadTimeout).",
      question: "Retry later.", suggested_changes: {},
    } } as never;
    render(createElement(TaskEditorProvider, null, createElement(EditorListener),
      createElement(RecoveryPanel, { result, onRetry: retry })));
    expect(apply).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Keep editing manually" })).not.toBeInTheDocument();
    expect(screen.getByText("Service options")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry same task" }));
    expect(retry).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "Use candidate CSV" }));
    expect(apply).toHaveBeenCalledWith({ candidate_source: "csv" });
  });

  it("prevents duplicate submissions while a retry is starting", () => {
    const result = { recovery: { diagnosis: "OpenStreetMap lookup failed", question: "Retry", suggested_changes: {} } } as never;
    render(createElement(TaskEditorProvider, null,
      createElement(RecoveryPanel, { result, onRetry: vi.fn(), retryPending: true })));
    expect(screen.getByRole("button", { name: /Starting retry/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Use candidate CSV" })).toBeDisabled();
  });

  it("allows retrying a solver timeout without changing the task", () => {
    const retry = vi.fn();
    const result = { recovery: {
      diagnosis: "The requested coverage solve reached its time limit without a verified solution.",
      question: "Retry the same task.", suggested_changes: {},
    } } as never;
    render(createElement(TaskEditorProvider, null,
      createElement(RecoveryPanel, { result, onRetry: retry })));
    fireEvent.click(screen.getByRole("button", { name: "Retry same task" }));
    expect(retry).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: "Apply to task editor" })).not.toBeInTheDocument();
  });
});
