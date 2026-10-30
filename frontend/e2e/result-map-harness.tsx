/// <reference lib="dom" />
import { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { RunResult, RunSnapshot } from "../src/api/models";
import { ResultTabs } from "../src/features/result/ResultTabs";
import { ResultPanel } from "../src/features/result/ResultPanel";
import { TaskEditorProvider } from "../src/features/task/TaskEditorContext";
import { taskFromDraft } from "../src/features/task/taskForm.mapper";
import "../src/styles/globals.css";

// eslint-disable-next-line react-refresh/only-export-components
function Harness() {
  const [diagnostic, setDiagnostic] = useState(true);
  const result = {
    task: taskFromDraft({ place_name: "Riverside", analysis_goal: diagnostic ? "minimum_threshold" : "facility_siting", mobility_scenario: "driving" }),
    artifacts: { map: "/test-map.geojson", ...(diagnostic ? { minimum_threshold_map: "/test-map.geojson" } : {}) },
  } as RunResult;
  return <section className="panel result-panel" style={{ height: "calc(100dvh - 40px)", maxHeight: "none", margin: 20 }}>
    <button type="button" onClick={() => setDiagnostic(!diagnostic)}>{diagnostic ? "Show ordinary analysis" : "Show coverage diagnostic"}</button>
    <ResultTabs runId="result-map-test" result={result} />
  </section>;
}

// eslint-disable-next-line react-refresh/only-export-components
function EstimateHarness() {
  const snapshot = {
    run_id: "estimate-map-test", status: "succeeded", error: "ORS request budget exceeded",
    result: {
      task: taskFromDraft({ place_name: "Riverside", analysis_goal: "minimum_facilities", threshold_minutes: 45, diagnostic_fixed_inputs: ["threshold_minutes"] }),
      message: "Estimated minimum: 10 facilities within 45 minutes.",
      execution: { mode: "auto", phase: "quick", status: "complete", accuracy: "approximate", router_requested: "haversine", router_used: "haversine" },
      diagnostics: { minimum_facilities: { feasible: true, minimum_facilities: 10, minimum_proven: true, threshold_minutes: 45 }, precise_refinement: { status: "unavailable", reason: "ORS request budget exceeded" } },
      artifacts: { map: "/test-map.geojson", minimum_facilities_map: "/test-map.geojson" },
    },
  } as unknown as RunSnapshot;
  return <main style={{ margin: 20 }}><ResultPanel snapshot={snapshot} /></main>;
}

createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={new QueryClient()}><TaskEditorProvider>{new URLSearchParams(location.search).has("estimate") ? <EstimateHarness /> : <Harness />}</TaskEditorProvider></QueryClientProvider>,
);
