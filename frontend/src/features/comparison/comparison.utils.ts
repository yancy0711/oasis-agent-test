import type { RunSnapshot } from "../../api/models";
import { publicApiUrl } from "../../api/client";
import { visibleResult } from "../run/run.selectors";

type Row = Record<string, unknown>;
const asRecord = (value: unknown): Row => value && typeof value === "object" && !Array.isArray(value) ? value as Row : {};
const numberOrNull = (value: unknown) => typeof value === "number" ? value : null;

export function runSummary(run: RunSnapshot) {
  const result = visibleResult(run); const metrics = result?.metrics ?? {};
  const optimization = (result?.results ?? []).find((item) => item.tool === "optimize_sites");
  const summary = asRecord(optimization?.summary);
  const rawSites = Array.isArray(summary.selected_sites) ? summary.selected_sites : [];
  const sites = rawSites.flatMap((item) => {
    const site = asRecord(item); const id = site.cand_id ?? site.id;
    return id == null ? [] : [{ id: String(id), name: typeof site.name === "string" && site.name ? site.name : `Candidate ${id}` }];
  });
  const selectedIds = sites.length ? sites.map((site) => site.id) : Array.isArray(summary.selected_ids) ? summary.selected_ids.map(String) : [];
  return { runId: run.run_id, place: run.task.place_name, mode: run.task.mobility_scenario, facilities: run.task.n_facilities, threshold: run.task.threshold_minutes,
    coverage: numberOrNull(metrics.coverage ?? summary.covered_ratio), vulnerable: numberOrNull(metrics.vulnerable ?? summary.vulnerable_covered_ratio),
    coveredTracts: numberOrNull(metrics.covered_tract_ratio ?? summary.covered_tract_ratio), averageTravel: numberOrNull(metrics.avg_walk ?? summary.mean_travel_minutes),
    accuracy: result?.execution.accuracy ?? null, weights: run.task.weights, selectedIds, sites,
    mapUrl: publicApiUrl(result?.artifacts?.map), diagnostics: asRecord(result?.diagnostics) };
}

export function formatPercent(value: number | null) { return value == null ? "—" : `${(value * 100).toFixed(1)}%`; }
export function jaccard(left: string[], right: string[]) { const a = new Set(left), b = new Set(right), union = new Set([...a, ...b]); return union.size ? [...a].filter((id) => b.has(id)).length / union.size : 1; }
