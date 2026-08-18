import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TaskEditorProvider } from "./TaskEditorContext";
import { TaskForm } from "./TaskForm";
import { taskFromDraft } from "./taskForm.mapper";

afterEach(cleanup);

describe("advanced task editor", () => {
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
