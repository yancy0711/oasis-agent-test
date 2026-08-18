import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { RunResult } from "../../api/models";
import { taskFromDraft } from "../task/taskForm.mapper";
import { ParetoView } from "./ParetoView";

afterEach(cleanup);

describe("interactive Pareto explorer", () => {
  it("selects a point for inspection without creating a revision", () => {
    const result = {
      ok: true, task: taskFromDraft({ place_name: "Phoenix", equity_alpha: 0 }),
      execution: { mode: "precise", phase: "precise", router_requested: "ors", status: "complete" },
      pareto_points: [
        { alpha: 0, efficiency: .064, equality: .034, gini: .966, covered_population_ratio: .10, vulnerable_coverage_ratio: .11, mean_access_score: .064, mean_travel_minutes: 8, selected_ids: ["a"], is_frontier: true },
        { alpha: 1, efficiency: .067, equality: .036, gini: .964, covered_population_ratio: .09, vulnerable_coverage_ratio: .10, mean_access_score: .067, mean_travel_minutes: 9, selected_ids: ["b"], is_frontier: true },
      ],
    } as unknown as RunResult;
    render(<QueryClientProvider client={new QueryClient()}><ParetoView runId="run-1" result={result} /></QueryClientProvider>);
    fireEvent.click(screen.getByRole("button", { name: /α = 1.0/ }));
    expect(screen.getByText("Equality emphasis")).toBeInTheDocument();
    expect(screen.getAllByText("0.964")).toHaveLength(2);
    expect(screen.queryByText("Balance reference")).not.toBeInTheDocument();
    expect(screen.getByText("Use this alpha in a new revision")).toBeEnabled();
  });

  it("marks an interior balance reference and previews it without applying it", () => {
    const onPreview = vi.fn();
    const result = {
      ok: true, task: taskFromDraft({ place_name: "Phoenix", equity_alpha: 0 }),
      execution: { mode: "precise", phase: "precise", router_requested: "ors", status: "complete" },
      pareto_points: [
        { alpha: 0, efficiency: .10, equality: .10, gini: .90, covered_population_ratio: .1, vulnerable_coverage_ratio: .1, mean_access_score: .1, mean_travel_minutes: 8, selected_ids: ["a"], selected_sites: [], is_frontier: true },
        { alpha: .5, efficiency: .14, equality: .30, gini: .70, covered_population_ratio: .1, vulnerable_coverage_ratio: .1, mean_access_score: .14, mean_travel_minutes: 9, selected_ids: ["b"], selected_sites: [{ id: "b", name: "Site B", longitude: -112, latitude: 33 }], is_frontier: true },
        { alpha: 1, efficiency: .15, equality: .50, gini: .50, covered_population_ratio: .1, vulnerable_coverage_ratio: .1, mean_access_score: .15, mean_travel_minutes: 10, selected_ids: ["c"], selected_sites: [], is_frontier: true },
      ],
    } as unknown as RunResult;
    render(<QueryClientProvider client={new QueryClient()}><ParetoView runId="run-2" result={result} onPreview={onPreview} /></QueryClientProvider>);
    fireEvent.click(screen.getByRole("button", { name: /α = 0.5/ }));
    expect(screen.getByText("Balance reference")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Preview these sites on map" }));
    expect(onPreview).toHaveBeenCalledWith(expect.objectContaining({ alpha: .5, selected_ids: ["b"] }));
  });

  it("keeps overlapping alpha solutions individually inspectable", () => {
    const result = {
      ok: true, task: taskFromDraft({ place_name: "Phoenix", equity_alpha: 0 }),
      execution: { mode: "precise", phase: "precise", router_requested: "ors", status: "complete" },
      pareto_points: [0, .5, 1].map((alpha) => ({
        alpha, efficiency: .038, equality: .066, gini: .934,
        covered_population_ratio: .05, vulnerable_coverage_ratio: .04,
        mean_access_score: .038, mean_travel_minutes: 9, selected_ids: ["a"],
        selected_sites: [], is_frontier: true,
      })),
    } as unknown as RunResult;
    render(<QueryClientProvider client={new QueryClient()}><ParetoView runId="run-overlap" result={result} /></QueryClientProvider>);
    const grouped = screen.getByRole("button", { name: /3 alpha settings with identical rounded metrics/ });
    fireEvent.mouseEnter(grouped);
    expect(screen.getByText(/3 α settings have the same rounded metrics, so they are represented by one point/)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /identical rounded metrics/ })).toHaveLength(1);
  });
});
