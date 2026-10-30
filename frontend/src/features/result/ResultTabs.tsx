import { lazy, Suspense, useRef, useState, type CSSProperties, type PointerEvent } from "react";
import type { RunResult } from "../../api/models";
import { publicApiUrl } from "../../api/client";
import type { ParetoPoint } from "./ParetoView";

const MapView = lazy(() => import("./MapView").then((module) => ({ default: module.MapView })));
const AnalysisView = lazy(() => import("./ResultDetails").then((module) => ({ default: module.AnalysisView })));
const ProvenanceView = lazy(() => import("./ResultDetails").then((module) => ({ default: module.ProvenanceView })));
const ParetoView = lazy(() => import("./ParetoView").then((module) => ({ default: module.ParetoView })));
const ReportView = lazy(() => import("./ReportView").then((module) => ({ default: module.ReportView })));
const ComparisonView = lazy(() => import("../comparison/ComparisonView").then((module) => ({ default: module.ComparisonView })));
type Tab = "map" | "analysis" | "pareto" | "compare" | "report" | "provenance";

export function ResultTabs({ runId, result }: { runId: string; result: RunResult }) {
  const [tab, setTab] = useState<Tab>("map");
  const [paretoPreview, setParetoPreview] = useState<ParetoPoint | null>(null);
  const [diagnosticMap, setDiagnosticMap] = useState<string | null>(null);
  const [focusTractId, setFocusTractId] = useState<string | null>(null);
  const [paretoWidth, setParetoWidth] = useState(48);
  const paretoWorkspaceRef = useRef<HTMLDivElement>(null);
  const resizePareto = (event: PointerEvent<HTMLDivElement>) => {
    if (!event.currentTarget.hasPointerCapture(event.pointerId)) return;
    const bounds = paretoWorkspaceRef.current?.getBoundingClientRect();
    if (!bounds) return;
    setParetoWidth(Math.max(35, Math.min(70, (event.clientX - bounds.left) / bounds.width * 100)));
  };
  const artifacts = result.artifacts ?? {};
  const extension = result.diagnostics?.hypothetical_coverage as Record<string, unknown> | undefined;
  const noExtensionMap = extension?.planning_mode === "fixed_budget" && !artifacts.hypothetical_coverage_map;
  const diagnosticMaps = [
    { key: "minimum_facilities_map", label: "Fixed time: minimum facilities" },
    { key: "minimum_threshold_map", label: "Fixed budget: minimum travel time" },
    { key: "coverage_alternative_map", label: "Alternative: longer travel threshold" },
    { key: "hypothetical_coverage_map", label: "Extension: hypothetical sites (approximate)" },
  ].filter((option) => Boolean(artifacts[option.key]));
  const primaryMap = diagnosticMaps.find((option) => option.key === `${result.task.analysis_goal}_map`)?.key ?? diagnosticMaps[0]?.key ?? "map";
  const activeMap = diagnosticMaps.some((option) => option.key === diagnosticMap) ? diagnosticMap! : primaryMap;
  return <div className="result-tabs"><div className="tab-list" role="tablist" aria-label="Analysis results">{(["map", "analysis", "pareto", "compare", "report", "provenance"] as Tab[]).map((name) => <button key={name} type="button" role="tab" aria-selected={tab === name} className={tab === name ? "is-active" : ""} onClick={() => setTab(name)}>{name}</button>)}</div><div className="tab-content">
    <Suspense fallback={<p className="empty-tab">Loading {tab} view<span className="animated-ellipsis" aria-label="in progress" /></p>}>
      {tab === "map" && <div className="result-map-panel">
        {noExtensionMap ? <p className="empty-tab">No extension map is available because no verified portfolio was produced. Review the Analysis tab and the confirmed inputs.</p> : <>
        {diagnosticMaps.length > 0 && <label className="field"><span>Coverage plan</span><select aria-label="Coverage plan" value={activeMap} onChange={(event) => setDiagnosticMap(event.target.value)}>{diagnosticMaps.map((option) => <option key={option.key} value={option.key}>{option.label}</option>)}</select></label>}
        {activeMap === "coverage_alternative_map" && <p className="notice">Alternative arrangement at a longer threshold. The original time requirement is not met.{(result.diagnostics?.coverage_alternative as Record<string, unknown> | undefined)?.accuracy === "approximate_haversine" ? " Approximate travel estimate; not ORS-verified." : ""}</p>}
        {activeMap === "hypothetical_coverage_map" && <p className="notice">Post-paper extension. Purple: hypothetical sites; blue: existing candidates. Approximate travel only; locations and network coverage are unverified.</p>}
        <MapView key={`${runId}:${activeMap}`} initialRouteMode={result.task.mobility_scenario === "driving" || result.task.mobility_scenario === "cycling" ? result.task.mobility_scenario : "walking"} runId={runId} url={publicApiUrl(artifacts[activeMap])} paretoPreview={paretoPreview} onClearParetoPreview={() => setParetoPreview(null)} showRoutePlanner={activeMap === primaryMap && activeMap !== "hypothetical_coverage_map"} focusTractId={focusTractId} routeOriginQuery={result.task.route_origin?.entity} routeOriginCoordinate={result.task.route_origin?.longitude != null && result.task.route_origin?.latitude != null ? { longitude: result.task.route_origin.longitude, latitude: result.task.route_origin.latitude } : null} />
        </>}
      </div>}
      {tab === "analysis" && <AnalysisView result={result} onShowTract={(geoid) => { setFocusTractId(geoid); setTab("map"); }} />}
      {tab === "pareto" && <div ref={paretoWorkspaceRef} className="pareto-workspace" style={{ "--pareto-left": `${paretoWidth}%` } as CSSProperties}><ParetoView runId={runId} result={result} url={publicApiUrl(artifacts.pareto)} onPreview={setParetoPreview} /><div className="pareto-resizer" role="separator" aria-label="Resize Pareto chart and map" aria-orientation="vertical" aria-valuemin={35} aria-valuemax={70} aria-valuenow={Math.round(paretoWidth)} tabIndex={0} onPointerDown={(event) => event.currentTarget.setPointerCapture(event.pointerId)} onPointerMove={resizePareto} onPointerUp={(event) => event.currentTarget.releasePointerCapture(event.pointerId)} onKeyDown={(event) => { if (event.key === "ArrowLeft") setParetoWidth((value) => Math.max(35, value - 3)); if (event.key === "ArrowRight") setParetoWidth((value) => Math.min(70, value + 3)); }} /><MapView runId={runId} url={publicApiUrl(artifacts.map)} paretoPreview={paretoPreview} onClearParetoPreview={() => setParetoPreview(null)} showRoutePlanner={false} /></div>}
      {tab === "compare" && <ComparisonView result={result} />}
      {tab === "report" && <ReportView runId={runId} url={publicApiUrl(artifacts.report)} narrative={result.narrative} recoveryText={result.recovery ? `${result.recovery.diagnosis} ${result.recovery.question}` : undefined} />}
      {tab === "provenance" && <ProvenanceView result={result} />}
    </Suspense>
  </div></div>;
}
