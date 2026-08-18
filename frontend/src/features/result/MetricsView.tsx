import type { RunResult } from "../../api/models";

function format(value: number | null | undefined, kind: "percent" | "minutes" | "number") {
  if (value == null) return "—";
  return kind === "percent" ? `${(value * 100).toFixed(1)}%`
    : kind === "minutes" ? `${value.toFixed(1)} min` : value.toFixed(3);
}

export function MetricsView({ result }: { result: RunResult }) {
  const metrics = result.metrics ?? {};
  const vulnerability = (result.diagnostics?.vulnerability_analysis ?? {}) as Record<string, unknown>;
  const missing = Array.isArray(vulnerability.missingness) ? vulnerability.missingness as Array<Record<string, unknown>> : [];
  const missingCount = missing.length ? missing.reduce((sum, row) => sum + Number(row.missing_count ?? 0), 0) : null;
  const missingDetail = missing.filter((row) => Number(row.missing_count ?? 0) > 0)
    .map((row) => `${String(row.indicator ?? "indicator").replaceAll("_", " ")}: ${row.missing_count} tract value, ${row.strategy ?? "configured"} handling`).join("; ");
  const equality = metrics.population_weighted_access_gini == null ? null : 1 - metrics.population_weighted_access_gini;
  return <div className="metric-groups">
    <section><h3>Efficiency</h3><div className="metrics"><div><span>Population coverage</span><strong>{format(metrics.coverage, "percent")}</strong></div><div><span>Gradual access</span><strong>{format(metrics.gradual_coverage, "percent")}</strong></div><div><span>Average travel</span><strong>{format(metrics.avg_walk, "minutes")}</strong></div></div></section>
    <section><h3>Equity</h3><div className="metrics"><div><span>Priority population</span><strong>{format(metrics.vulnerable, "percent")}</strong></div><div><span>Access Gini</span><strong>{format(metrics.population_weighted_access_gini, "number")}</strong></div><div><span>Access equality</span><strong>{format(equality, "percent")}</strong></div></div></section>
    <section><h3>Data quality</h3><div className="metrics"><div><span>Routing evidence</span><strong>{result.execution.accuracy === "network" ? "ORS network" : "Approximate"}</strong></div><div title={missingDetail || "No configured vulnerability indicator values were missing."}><span>Missing indicator values</span><strong>{missingCount ?? "Not audited"}</strong><small>{missingCount == null ? "No missing-value audit was published." : missingDetail || "None across configured vulnerability indicators."}</small></div><div><span>Run status</span><strong>{result.execution.status}</strong></div></div></section>
  </div>;
}
