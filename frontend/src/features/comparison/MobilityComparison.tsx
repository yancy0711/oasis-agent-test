import { useAppStore } from "../../store/appStore";
import { DataTable } from "../result/ResultShared";
import { useComparisonRuns } from "./comparison.queries";
import { formatPercent, jaccard, runSummary } from "./comparison.utils";

export function MobilityComparison() {
  const mobilityRunIds = useAppStore((state) => state.mobilityRunIds); const signature = useAppStore((state) => state.mobilitySignature);
  const runIds = Object.values(mobilityRunIds).filter((value): value is string => Boolean(value)); const queries = useComparisonRuns(runIds);
  const summaries = queries.flatMap((query) => query.data ? [runSummary(query.data)] : []);
  if (runIds.length < 2) return <p className="empty-tab">Complete matching runs with at least two mobility modes.</p>;
  const siteNames = new Map(summaries.flatMap((item) => item.sites.map((site) => [site.id, site.name] as const)));
  const membership = [...siteNames].map(([id, name]) => ({ Site: name, ...Object.fromEntries(summaries.map((item) => [item.mode.replaceAll("_", " "), item.selectedIds.includes(id) ? "Selected" : "—"])) }));
  const overlap = summaries.map((left) => ({ Mode: left.mode.replaceAll("_", " "), ...Object.fromEntries(summaries.map((right) => [right.mode.replaceAll("_", " "), formatPercent(jaccard(left.selectedIds, right.selectedIds))])) }));
  const stability = [...siteNames].map(([id, name]) => ({ Site: name, modes_selected: summaries.filter((item) => item.selectedIds.includes(id)).length })).sort((a, b) => b.modes_selected - a.modes_selected);
  return <div className="detail-stack"><p className="muted">Only runs with identical non-mobility parameters are grouped. Group {signature?.slice(0, 18)}…</p><div className="comparison-cards comparison-cards--mobility">{summaries.map((item) => <article key={item.runId}><span>{item.mode.replaceAll("_", " ")}</span><strong>{formatPercent(item.coverage)} coverage</strong><small>{item.facilities} facilities · {item.threshold} min · {item.accuracy ?? "unknown accuracy"}</small></article>)}</div>
    {membership.length > 0 && <section><h3>Selected-site membership</h3><DataTable data={membership} /></section>}<section><h3>Selection overlap (Jaccard)</h3><DataTable data={overlap} /></section>{stability.length > 0 && <section><h3>Site stability</h3><DataTable data={stability} /></section>}
  </div>;
}
