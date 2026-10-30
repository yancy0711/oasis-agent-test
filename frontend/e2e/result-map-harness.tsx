/// <reference lib="dom" />
import { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { RunResult } from "../src/api/models";
import { ResultTabs } from "../src/features/result/ResultTabs";
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

createRoot(document.getElementById("root")!).render(
  <QueryClientProvider client={new QueryClient()}><Harness /></QueryClientProvider>,
);
