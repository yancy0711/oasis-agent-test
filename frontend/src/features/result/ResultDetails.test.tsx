import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { RunResult } from "../../api/models";
import { AnalysisView } from "./ResultDetails";
import { ExecutionSummary } from "./ResultShared";
import { taskFromDraft } from "../task/taskForm.mapper";

afterEach(cleanup);

describe("facility-siting analysis", () => {
  it("renders wide routing metadata as a contained two-column field/value table", () => {
    const result = {
      ok: true, paused: false, message: "done", task: taskFromDraft({ place_name: "Phoenix" }),
      execution: { mode: "precise", phase: "precise", router_requested: "ors", router_used: "ors_sparse", accuracy: "network", status: "complete" },
      routing_status: { router: "ors_sparse", modes: ["walking"], mode_reports: { walking: { tier: "live+cache", total_pairs: 1800326 } } },
    } as RunResult;
    render(<ExecutionSummary result={result} />);
    fireEvent.click(screen.getByText("Routing status"));
    expect(screen.getAllByRole("columnheader").map((item) => item.textContent)).toEqual(["field", "value"]);
    expect(screen.getByText("mode reports")).toBeInTheDocument();
    expect(screen.getByText("1 fields")).toBeInTheDocument();
  });

  it("hides empty regional constraint bookkeeping", () => {
    const result = {
      ok: true, paused: false, message: "done", task: taskFromDraft({ place_name: "Phoenix" }),
      execution: { mode: "precise", phase: "precise", router_requested: "ors", router_used: "ors", accuracy: "network", status: "complete" },
      diagnostics: { constraint_validation: { global_budget: 8, regional_requirements: [], conflicts: [], requires_region_geometry: false } },
    } as RunResult;
    render(<AnalysisView result={result} />);
    expect(screen.queryByRole("heading", { name: "Constraint validation" })).not.toBeInTheDocument();
  });

  it("uses tool summaries and constraint diagnostics instead of showing an empty result", () => {
    const result = {
      ok: true, paused: false, message: "done", task: taskFromDraft({ place_name: "Phoenix" }),
      execution: { mode: "precise", phase: "precise", router_requested: "ors", router_used: "ors", accuracy: "network", status: "complete" },
      diagnostics: { constraint_validation: { feasible: true, violations: 0 } },
      results: [
        { tool: "build_travel_matrix", ok: true, summary: { router: "ors", reachable_pairs: 120 } },
        { tool: "optimize_sites", ok: true, summary: { selected_ids: ["a", "b"], covered_ratio: .82 } },
      ],
    } as RunResult;
    render(<AnalysisView result={result} />);
    expect(screen.getByRole("heading", { name: "Optimization result" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Constraint validation" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Travel matrix analysis" })).toBeInTheDocument();
    expect(screen.queryByText("No additional diagnostics were produced for this run.")).not.toBeInTheDocument();
  });

  it("shows vulnerability correlation, PCA, and robustness diagnostics", () => {
    const result = {
      ok: true, paused: false, message: "done", task: taskFromDraft({ place_name: "Phoenix" }),
      execution: { mode: "precise", phase: "precise", router_requested: "ors", router_used: "ors", accuracy: "network", status: "complete" },
      diagnostics: { vulnerability_analysis: {
        tract_count: 4, priority_tracts: 1, priority_definition: "top 25% of the active composite vulnerability score",
        active_scheme: "configured", normalization: "percentile",
        missingness: [{ indicator: "elderly_ratio", missing_count: 1, missing_percent: .25, strategy: "median" }],
        correlation: { indicators: ["elderly_ratio", "poverty_rate"], matrix: [[1, .8], [.8, 1]], high_pairs: [] },
        vif: [{ indicator: "elderly_ratio", vif: 2 }],
        pca: { explained_variance_ratio: [.7, .3], pc1_loadings: { elderly_ratio: .7, poverty_rate: .7 } },
        diagnostic_correlation: { indicators: ["heat_exposure", "mean_ndvi"], matrix: [[1, -.7], [-.7, 1]], strongest_pairs: [{ features: "heat_exposure and mean_ndvi", correlation: -.7, paired_tracts: 4 }] },
        scheme_descriptions: { configured: "Audited defaults" },
        weight_sensitivity: { mean_rank_standard_deviation: .03, comparisons: [{ schemes: "configured vs equal", rank_correlation: .95, priority_tract_jaccard: 1 }], schemes: { configured: { elderly_ratio: .5, poverty_rate: .5 } } },
      } },
    } as RunResult;
    render(<AnalysisView result={result} />);
    expect(screen.getByRole("heading", { name: "Vulnerability data quality and robustness" })).toBeInTheDocument();
    expect(screen.getByText("Feature correlation")).toBeInTheDocument();
    expect(screen.getByText("70.0%")).toBeInTheDocument();
    expect(screen.getByText("Vulnerability, temperature, and built environment")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Missingness, collinearity, and weight-scheme details"));
    expect(screen.getByText("Weight sensitivity")).toBeInTheDocument();
  });
});
