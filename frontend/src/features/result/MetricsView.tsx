import { useWorkspacePreferences } from "../../app/WorkspacePreferences";
import { createPortal } from "react-dom";
import type { RunResult } from "../../api/models";

function format(value: number | null | undefined, kind: "percent" | "minutes" | "number") {
  if (value == null) return "—";
  return kind === "percent" ? `${(value * 100).toFixed(1)}%`
    : kind === "minutes" ? `${value.toFixed(1)} min` : value.toFixed(3);
}

export function MetricsView({ result, dataQualitySlot }: { result: RunResult; dataQualitySlot?: HTMLDivElement | null }) {
  const { t } = useWorkspacePreferences();
  const metrics = result.metrics ?? {};
  const vulnerability = (result.diagnostics?.vulnerability_analysis ?? {}) as Record<string, unknown>;
  const missing = Array.isArray(vulnerability.missingness) ? vulnerability.missingness as Array<Record<string, unknown>> : [];
  const excluded = Array.isArray(vulnerability.excluded_indicators) ? vulnerability.excluded_indicators as Array<Record<string, unknown>> : [];
  const excludedDetail = excluded.map((row) => `${String(row.indicator).replaceAll("_", " ")} (${String(row.reason)})`).join(", ");
  const missingCount = missing.length ? missing.reduce((sum, row) => sum + Number(row.missing_count ?? 0), 0) : null;
  const missingDetail = missing.filter((row) => Number(row.missing_count ?? 0) > 0)
    .map((row) => `${String(row.indicator ?? "indicator").replaceAll("_", " ")}: ${row.missing_count} tract value, ${row.strategy ?? "configured"} handling`).join("; ");
  const equality = metrics.population_weighted_access_gini == null ? null : 1 - metrics.population_weighted_access_gini;
  const quality = <section><h3>{t("Data quality")}</h3><div className="metrics">
    <div><span>Routing evidence</span><strong>{result.execution.accuracy === "network" ? "ORS network" : "Approximate"}</strong></div>
    <div><span>Missing indicator values</span><strong>{missingCount ?? "—"}</strong><small>{missingDetail || (missingCount === 0 ? "None across retained vulnerability inputs." : "No retained indicator audit.")}</small></div>
    {excluded.length > 0 && <div><span>Vulnerability inputs excluded</span><strong>{excluded.length}</strong><small>{excludedDetail}</small></div>}
    <div><span>Run status</span><strong>{result.execution.status}</strong></div>
  </div></section>;
  return <div className="metric-groups">
    <section><h3>{t("Efficiency")}</h3><div className="metrics"><div><span>{t("Population coverage")}</span><strong>{format(metrics.coverage, "percent")}</strong></div><div><span>{t("Gradual access")}</span><strong>{format(metrics.gradual_coverage, "percent")}</strong></div><div><span>{t("Average travel")}</span><strong>{format(metrics.avg_walk, "minutes")}</strong></div></div></section>
    <section><h3>{t("Equity")}</h3><div className="metrics"><div><span>{t("Priority population")}</span><strong>{format(vulnerability.status === "unavailable" ? null : metrics.vulnerable, "percent")}</strong></div><div><span>{t("Access Gini")}</span><strong>{format(metrics.population_weighted_access_gini, "number")}</strong></div><div><span>{t("Access equality")}</span><strong>{format(equality, "percent")}</strong></div></div></section>
    {dataQualitySlot ? createPortal(quality, dataQualitySlot) : quality}
  </div>;
}
