import { lazy, Suspense, useState } from "react";
import type { RunResult } from "../../api/models";
import { publicApiUrl } from "../../api/client";
import { DataTable } from "../result/ResultShared";
import { MobilityComparison } from "./MobilityComparison";
import { ScenarioComparison } from "./ScenarioComparison";

const ComparisonMap = lazy(() => import("./ComparisonMap").then((module) => ({ default: module.ComparisonMap })));

const record = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const rows = (value: unknown): Record<string, unknown>[] => Array.isArray(value) ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object" && !Array.isArray(item)) : [];

export function ComparisonView({ result }: { result: RunResult }) {
  const [mode, setMode] = useState<"scenario" | "mobility">("scenario");
  const withinRun = record(record(result.diagnostics).scenario_comparison);
  const profileScenarios = rows(withinRun.scenarios);
  const profileComparisons = rows(withinRun.comparisons);
  return (
    <div>
      <div className="subtabs">
        <button type="button" className={mode === "scenario" ? "is-active" : ""} onClick={() => setMode("scenario")}>Profiles / scenarios</button>
        <button type="button" className={mode === "mobility" ? "is-active" : ""} onClick={() => setMode("mobility")}>Mobility modes</button>
      </div>
      {mode === "scenario" ? <>
        {profileScenarios.length > 0 && <section className="detail-stack">
          <div><h3>Profiles in this run</h3><p className="muted">Each profile uses the same study area, candidates, travel matrix, facility budget, and threshold; only the confirmed value weights differ.</p><DataTable data={profileScenarios} /></div>
          {result.artifacts?.map && <Suspense fallback={<p className="muted">Loading comparison map…</p>}><ComparisonMap url={publicApiUrl(result.artifacts.map)} baselineLabel={String(profileScenarios[0]?.profile_label ?? "Baseline")} alternativeLabel={String(profileScenarios[1]?.profile_label ?? "Alternative")} /></Suspense>}
          {profileComparisons.length > 0 && <div><h3>Changes from baseline</h3><DataTable data={profileComparisons} /></div>}
        </section>}
        <ScenarioComparison />
      </> : <MobilityComparison />}
    </div>
  );
}
