import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TaskEditorProvider } from "./TaskEditorContext";
import { TaskForm } from "./TaskForm";
import { taskFromDraft } from "./taskForm.mapper";

afterEach(cleanup);

describe("advanced task editor", () => {
  it("keeps route tasks simple while leaving advanced controls available", () => {
    const task = taskFromDraft({
      place_name: "Phoenix",
      route_origin: { entity: "Papago Park", context: "Phoenix", positional_hint: "", raw_text: "Papago Park", source: "llm" },
    });
    const { container } = render(<TaskEditorProvider><TaskForm task={task} mode="auto" ready pending={false} uploadPending={false} uploadedName={null}
      profiles={[]} onModeChange={() => undefined} onUpload={() => undefined} onSubmit={() => undefined} /></TaskEditorProvider>);
    const step = container.querySelector(".task-step-disclosure") as HTMLDetailsElement;
    expect(screen.getByText("Step 3")).toBeInTheDocument();
    expect(step.open).toBe(true);
    expect(screen.getByRole("button", { name: "Run confirmed agent" })).toBeInTheDocument();
    expect(screen.getByText(/Supporting defaults:/)).toBeInTheDocument();
    const advanced = container.querySelector(".task-settings-disclosure") as HTMLDetailsElement;
    expect(advanced.open).toBe(true);
  });

  it("submits untouched default weights", async () => {
    const submit = vi.fn();
    render(<TaskEditorProvider><TaskForm task={taskFromDraft({ place_name: "Phoenix" })} mode="auto" ready pending={false} uploadPending={false} uploadedName={null}
      profiles={[]} onModeChange={() => undefined} onUpload={() => undefined} onSubmit={submit} /></TaskEditorProvider>);
    expect(screen.getByText("6 of 6 types selected")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "OpenStreetMap candidate facility types" }));
    expect(screen.getByRole("checkbox", { name: "Public library" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Place of worship" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "School" })).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Select all" }));
    expect(screen.getByText("6 of 6 types selected")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Restore defaults" }));
    fireEvent.click(screen.getByRole("button", { name: "Run confirmed agent" }));
    await waitFor(() => expect(submit).toHaveBeenCalledOnce());
    expect(submit.mock.calls[0][0].weights).toEqual({ population_density: .35, elderly_ratio: .15, poverty_rate: .15, heat_exposure: .2, environment_burden: .15 });
    expect(submit.mock.calls[0][0].candidate_amenities).toEqual(["library", "community_centre", "school", "sports_centre", "arts_centre", "place_of_worship"]);
  });

  it("shows read-only result questions separately from the facility budget", () => {
    const task = taskFromDraft({
      place_name: "Washington, District of Columbia, USA",
      n_facilities: 10,
      result_queries: [{
        metric: "combined_disadvantage", direction: "highest", limit: 5,
        scope: "all", raw_text: "Which five places need the most attention?",
      }],
    });
    render(<TaskEditorProvider><TaskForm task={task} mode="auto" ready pending={false} uploadPending={false} uploadedName={null}
      profiles={[]} onModeChange={() => undefined} onUpload={() => undefined} onSubmit={() => undefined} /></TaskEditorProvider>);

    expect(screen.getByText("5 tract(s) by combined disadvantage (highest)")).toBeInTheDocument();
    expect(screen.getByText(/do not change the facility budget/i)).toBeInTheDocument();
  });

  it("offers direct choices for a typed model ambiguity", () => {
    const choose = vi.fn();
    const task = taskFromDraft({
      place_name: "San Francisco, California, USA",
      ambiguities: [{
        field: "analysis_goal",
        candidates: ["facility_siting", "minimum_facilities"],
        question: "Is the count a fixed budget or a minimum-number question?",
      }],
    });
    render(<TaskEditorProvider><TaskForm task={task} mode="auto" ready pending={false} uploadPending={false} uploadedName={null}
      profiles={[]} onModeChange={() => undefined} onUpload={() => undefined}
      onResolveAmbiguity={choose} onSubmit={() => undefined} /></TaskEditorProvider>);

    fireEvent.click(screen.getByRole("button", { name: "minimum facilities" }));
    expect(choose).toHaveBeenCalledWith(0, "minimum_facilities");
  });

  it("applies recommendations and submits operating and sensitivity controls", async () => {
    const submit = vi.fn();
    const task = taskFromDraft({
      place_name: "Phoenix", analysis_goal: "sensitivity_analysis",
      recommended_mobility_scenario: "cycling", recommended_value_profile: "equity_first",
      recommended_sensitivity_thresholds: [10, 20], recommended_sensitivity_facility_counts: [4, 8],
      recommendation_rationale: "Test both budgets.",
    });
    const { container } = render(<TaskEditorProvider><TaskForm task={task} mode="auto" ready pending={false} uploadPending={false} uploadedName={null}
      profiles={[{ name: "equity_first", label: "Equity first", weights: { population_density: .1, elderly_ratio: .3, poverty_rate: .3, heat_exposure: .3, environment_burden: 0 } }]}
      onModeChange={() => undefined} onUpload={() => undefined} onSubmit={submit} /></TaskEditorProvider>);

    fireEvent.click(screen.getByRole("button", { name: "Apply all recommendations" }));
    expect(container.querySelector('[name="mobility_scenario"]')).toHaveValue("cycling");
    expect(screen.getByRole("button", { name: "10 ×" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "8 ×" })).toBeInTheDocument();

    const population = container.querySelector('[name="weights.population_density"]')!;
    const poverty = container.querySelector('[name="weights.poverty_rate"]')!;
    fireEvent.change(population, { target: { value: "0" } });
    expect(screen.getByText("Add 0.10 before running.")).toBeInTheDocument();
    fireEvent.change(poverty, { target: { value: "0.5" } });
    expect(poverty).toHaveValue(.3);
    fireEvent.change(poverty, { target: { value: "0.4" } });
    expect(screen.getByText("Weights are ready.")).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText(/Filter candidate availability/));
    expect(screen.getAllByRole("button", { name: "More information" }).length).toBeGreaterThan(0);
    expect(screen.getByRole("tooltip", { name: /Filters candidate facilities using the selected local date/ })).toBeInTheDocument();
    fireEvent.change(container.querySelector('[name="service_date"]')!, { target: { value: "2026-08-05" } });
    fireEvent.change(container.querySelector('[name="service_start"]')!, { target: { value: "09:00" } });
    fireEvent.change(container.querySelector('[name="service_end"]')!, { target: { value: "17:00" } });
    expect(screen.queryByRole("button", { name: "Add regional requirement" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Run confirmed agent" }));

    await waitFor(() => expect(submit).toHaveBeenCalledOnce());
    const values = submit.mock.calls[0][0];
    expect(values.operating_window_enabled).toBe(true);
    expect(values.regional_requirements).toEqual([]);
    expect(values.sensitivity_thresholds).toEqual([10, 20]);
  });

  it("shows and submits registered profiles for a scenario comparison", async () => {
    const submit = vi.fn();
    const task = taskFromDraft({
      place_name: "Phoenix", analysis_goal: "scenario_comparison",
      comparison_profiles: ["efficiency_first", "equity_first"],
    });
    render(<TaskEditorProvider><TaskForm task={task} mode="quick" ready pending={false} uploadPending={false} uploadedName={null}
      profiles={[
        { name: "efficiency_first", label: "Efficiency first", weights: { population_density: 1, elderly_ratio: 0, poverty_rate: 0, heat_exposure: 0, environment_burden: 0 } },
        { name: "equity_first", label: "Equity first", weights: { population_density: .1, elderly_ratio: .3, poverty_rate: .3, heat_exposure: .3, environment_burden: 0 } },
      ]} onModeChange={() => undefined} onUpload={() => undefined} onSubmit={submit} /></TaskEditorProvider>);
    expect(screen.getByText("2 profiles selected")).toBeInTheDocument();
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Run confirmed agent" }));
    await waitFor(() => expect(submit).toHaveBeenCalledOnce());
    expect(submit.mock.calls[0][0].comparison_profiles).toEqual(["efficiency_first", "equity_first"]);
  });
});
