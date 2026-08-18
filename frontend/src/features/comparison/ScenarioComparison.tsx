import { lazy, Suspense, useEffect, useState } from "react";
import { InfoLabel } from "../../components/InfoLabel";
import { useAppStore } from "../../store/appStore";
import { DataTable } from "../result/ResultShared";
import { useComparisonRuns } from "./comparison.queries";
import { formatPercent, jaccard, runSummary } from "./comparison.utils";

const ComparisonMap = lazy(() => import("./ComparisonMap").then((module) => ({ default: module.ComparisonMap })));

function weightsLabel(weights: Record<string, number> | null | undefined) {
  if (!weights) return "No confirmed weights";
  return Object.entries(weights)
    .map(([name, value]) => `${name.replaceAll("_", " ")} ${value.toFixed(2)}`)
    .join(" · ");
}

export function ScenarioComparison() {
  const completed = useAppStore((state) => state.completedRunIds);
  const stored = useAppStore((state) => state.comparisonRunIds);
  const setStored = useAppStore((state) => state.setComparisonRunIds);
  const [left, setLeft] = useState("");
  const [right, setRight] = useState("");

  useEffect(() => {
    if (completed.length < 2) return;
    const storedValid = stored.length === 2 && stored.every((runId) => completed.includes(runId));
    const desired = storedValid ? stored : completed.slice(-2);
    setLeft((current) => completed.includes(current) ? current : desired[0]);
    setRight((current) => completed.includes(current) && current !== desired[0] ? current : desired[1]);
  }, [completed, stored]);

  useEffect(() => {
    if (left && right && left !== right) setStored([left, right]);
  }, [left, right, setStored]);

  const selectedIds = left && right && left !== right ? [left, right] : [];
  const queries = useComparisonRuns(selectedIds);
  if (completed.length < 2) return <p className="empty-tab">Complete two successful runs to compare scenarios.</p>;
  if (selectedIds.length < 2 || queries.some((query) => query.isLoading)) {
    return <p className="empty-tab">Loading the two comparison runs…</p>;
  }
  const failed = queries.find((query) => query.isError);
  if (failed) return <p className="error error--block">{failed.error?.message ?? "Could not load a comparison run."}</p>;

  const summaries = queries.flatMap((query) => query.data ? [runSummary(query.data)] : []);
  const [baseline, alternative] = summaries;
  const deltas = baseline && alternative ? [{
    coverage: alternative.coverage != null && baseline.coverage != null ? alternative.coverage - baseline.coverage : null,
    vulnerable: alternative.vulnerable != null && baseline.vulnerable != null ? alternative.vulnerable - baseline.vulnerable : null,
    covered_tract_ratio: alternative.coveredTracts != null && baseline.coveredTracts != null ? alternative.coveredTracts - baseline.coveredTracts : null,
    average_travel_minutes: alternative.averageTravel != null && baseline.averageTravel != null ? alternative.averageTravel - baseline.averageTravel : null,
    selected_site_jaccard: jaccard(baseline.selectedIds, alternative.selectedIds),
  }] : [];

  return <div className="comparison-view">
    <div className="comparison-selectors">{[{ value: left, set: setLeft }, { value: right, set: setRight }].map((entry, index) => {
      const label = index === 0 ? "Baseline" : "Alternative";
      return <label className="field" key={label}><InfoLabel label={label}>{index === 0 ? "Selects the reference run against which changes are measured." : "Selects the run whose settings and outcomes are compared with the baseline."}</InfoLabel><select aria-label={label} value={entry.value} onChange={(event) => entry.set(event.target.value)}><option value="">Select a run</option>{completed.map((runId) => <option key={runId} value={runId}>{runId.slice(0, 8)}</option>)}</select></label>;
    })}</div>
    <div className="comparison-cards">{summaries.map((item) => <article key={item.runId}><span>{item.runId.slice(0, 8)} · {item.mode} · {item.accuracy ?? "unknown accuracy"}</span><strong>{item.place}</strong><small>{weightsLabel(item.weights)}</small><dl><div><dt>Facilities</dt><dd>{item.facilities}</dd></div><div><dt>Threshold</dt><dd>{item.threshold} min</dd></div><div><dt>Coverage</dt><dd>{formatPercent(item.coverage)}</dd></div><div><dt>Priority population</dt><dd>{formatPercent(item.vulnerable)}</dd></div></dl></article>)}</div>
    {baseline?.mapUrl && alternative?.mapUrl && <Suspense fallback={<p className="muted">Loading comparison map…</p>}><ComparisonMap baselineUrl={baseline.mapUrl} alternativeUrl={alternative.mapUrl} baselineLabel={`Baseline ${baseline.runId.slice(0, 8)}`} alternativeLabel={`Alternative ${alternative.runId.slice(0, 8)}`} /></Suspense>}
    {deltas.length > 0 && <section><h3>Alternative − baseline</h3><DataTable data={deltas} /></section>}
    {alternative && Object.keys(alternative.diagnostics.scenario_comparison && typeof alternative.diagnostics.scenario_comparison === "object" ? alternative.diagnostics.scenario_comparison as object : {}).length > 0 && <details><summary>Within-run deterministic comparison</summary><pre className="json-view">{JSON.stringify(alternative.diagnostics.scenario_comparison, null, 2)}</pre></details>}
  </div>;
}
