import { useQuery } from "@tanstack/react-query";
import type { RunResult } from "../../api/models";
import { getDataSourceStatus, getOfficialResources, publicApiUrl } from "../../api/client";
import { DataTable } from "./ResultShared";

type Row = Record<string, unknown>;
const record = (value: unknown): Row => value && typeof value === "object" && !Array.isArray(value) ? value as Row : {};
const rows = (value: unknown): Row[] => Array.isArray(value) ? value.filter((item): item is Row => Boolean(item) && typeof item === "object" && !Array.isArray(item)) : [];
const display = (value: unknown) => value == null ? "—" : typeof value === "number" ? Number.isInteger(value) ? String(value) : value.toFixed(3) : typeof value === "object" ? JSON.stringify(value) : String(value);

function CorrelationHeatmap({ data }: { data: Row }) {
  const indicators = Array.isArray(data.indicators) ? data.indicators.map(String) : [];
  const matrix = Array.isArray(data.matrix) ? data.matrix as unknown[][] : [];
  if (!indicators.length || matrix.length !== indicators.length) return null;
  const color = (value: number) => value >= 0
    ? `rgba(25, 77, 58, ${0.08 + Math.abs(value) * 0.78})`
    : `rgba(165, 65, 53, ${0.08 + Math.abs(value) * 0.78})`;
  return <div className="correlation-wrap"><div className="correlation-heatmap" style={{ gridTemplateColumns: `minmax(108px, auto) repeat(${indicators.length}, minmax(64px, 1fr))` }}>
    <span />{indicators.map((name) => <strong key={`column-${name}`} title={name}>{name.replaceAll("_", " ")}</strong>)}
    {indicators.flatMap((name, rowIndex) => [
      <strong key={`row-${name}`}>{name.replaceAll("_", " ")}</strong>,
      ...indicators.map((column, columnIndex) => {
        const raw = matrix[rowIndex]?.[columnIndex];
        const value = raw == null ? Number.NaN : Number(raw);
        return <span key={`${name}-${column}`} title={`${name} vs ${column}: ${Number.isFinite(value) ? value.toFixed(3) : "unavailable"}`} style={{ background: Number.isFinite(value) ? color(value) : "#eef1ee", color: Math.abs(value) > .62 ? "white" : "var(--ink)" }}>{Number.isFinite(value) ? value.toFixed(2) : "—"}</span>;
      }),
    ])}
  </div><p className="heatmap-key"><span>negative</span><i /><span>0</span><i /><span>positive</span></p></div>;
}

function PcaSummary({ data }: { data: Row }) {
  const ratios = Array.isArray(data.explained_variance_ratio) ? data.explained_variance_ratio.map(Number).filter(Number.isFinite) : [];
  const loadings = record(data.pc1_loadings);
  if (!ratios.length) return null;
  return <div className="diagnostic-grid"><div><h4>Explained variance</h4><div className="pca-bars">{ratios.map((ratio, index) => <div key={index}><span>PC{index + 1}</span><i><b style={{ width: `${Math.max(0, Math.min(1, ratio)) * 100}%` }} /></i><strong>{(ratio * 100).toFixed(1)}%</strong></div>)}</div></div><div><h4>PC1 loadings</h4><DataTable data={Object.entries(loadings).map(([indicator, loading]) => ({ indicator, loading }))} /></div></div>;
}

function informativeValidation(value: Row) {
  const standard = new Set(["global_budget", "regional_requirements", "conflicts", "requires_region_geometry"]);
  return rows(value.regional_requirements).length > 0 || rows(value.conflicts).length > 0
    || value.requires_region_geometry === true || Object.keys(value).some((key) => !standard.has(key));
}

export function AnalysisView({ result, onShowTract }: { result: RunResult; onShowTract?: (geoid: string) => void }) {
  const diagnostics = record(result.diagnostics);
  const sensitivity = record(diagnostics.sensitivity_results);
  const experiments = rows(sensitivity.experiments);
  const heat = record(result.heat_summary);
  const topTracts = rows(heat.top_tracts);
  const facilities = record(diagnostics.minimum_facilities);
  const threshold = record(diagnostics.minimum_threshold);
  const alternative = record(diagnostics.coverage_alternative);
  const extension = record(diagnostics.hypothetical_coverage);
  const approximateThreshold = record(diagnostics.approximate_minimum_threshold);
  const reachability = record(diagnostics.reachability_analysis);
  const resultQueries = rows(record(diagnostics.result_query_answers).answers);
  const rankedTracts = resultQueries.flatMap((answer) => rows(answer.top_rows));
  const reachabilityOverview = Object.keys(reachability).length ? {
    coverage_group: reachability.coverage_group,
    threshold_minutes: reachability.threshold_minutes,
    maximum_tract_coverage: reachability.maximum_tract_coverage,
    unserved_demand_units: Array.isArray(reachability.unserved_demand_ids) ? reachability.unserved_demand_ids.length : 0,
    structurally_unreachable_units: Array.isArray(reachability.structurally_unreachable_ids) ? reachability.structurally_unreachable_ids.length : 0,
  } : {};
  const scenario = record(diagnostics.scenario_comparison);
  const validation = record(diagnostics.constraint_validation);
  const vulnerability = record(diagnostics.vulnerability_analysis);
  const vulnerabilityCorrelation = record(vulnerability.correlation);
  const vulnerabilityPca = record(vulnerability.pca);
  const vulnerabilitySensitivity = record(vulnerability.weight_sensitivity);
  const diagnosticCorrelation = record(vulnerability.diagnostic_correlation);
  const schemeDescriptions = record(vulnerability.scheme_descriptions);
  const showValidation = informativeValidation(validation);
  const optimizationResult = (result.results ?? []).find((item) => item.tool === "optimize_sites");
  const routingResult = (result.results ?? []).find((item) => item.tool === "build_travel_matrix");
  const optimization = record(optimizationResult?.summary);
  const routing = record(routingResult?.summary);
  const failures = (result.results ?? []).filter((item) => item.error || item.ok === false);
  const metrics = result.metrics ?? {};
  const missingTotal = rows(vulnerability.missingness).reduce((sum, item) => sum + Number(item.missing_count ?? 0), 0);
  const metricPercent = (value: unknown) => typeof value === "number" ? `${(value * 100).toFixed(1)}%` : "—";
  const heatUrl = publicApiUrl(result.artifacts?.heat_analysis);
  const hasAny = experiments.length || topTracts.length || rankedTracts.length || Object.keys(facilities).length || Object.keys(threshold).length || Object.keys(approximateThreshold).length || Object.keys(reachability).length || Object.keys(scenario).length || Object.keys(vulnerability).length || showValidation || Object.keys(optimization).length || Object.keys(routing).length || failures.length || heatUrl;
  if (!hasAny) return <p className="empty-tab">No additional diagnostics were produced for this run.</p>;
  return <div className="detail-stack">
    <section className="analysis-overview"><h3>Decision summary</h3><p className="muted">Current outcome first; open the diagnostic sections only when you need methodological detail.</p>
      {(topTracts.length > 0 || rankedTracts.length > 0 || Object.keys(reachability).length > 0 || Object.keys(facilities).length > 0 || Object.keys(threshold).length > 0 || Object.keys(approximateThreshold).length > 0) && <p className="diagnostic-answer">{result.message}</p>}
      <div className="diagnostic-summary"><div><span>Population coverage</span><strong>{metricPercent(metrics.coverage)}</strong></div><div><span>Gradual access</span><strong>{metricPercent(metrics.gradual_coverage)}</strong></div><div><span>Access Gini</span><strong>{display(metrics.population_weighted_access_gini)}</strong></div><div><span>Missing values</span><strong>{Object.keys(vulnerability).length ? missingTotal : "not audited"}</strong></div></div>
    </section>
    {Object.keys(vulnerability).length > 0 && <section className="vulnerability-analysis"><h3>Vulnerability data quality and robustness</h3><p className="muted">{display(vulnerability.priority_definition)} · active weighting: {display(vulnerability.active_scheme)} · normalization: {display(vulnerability.normalization)}. Indicator directions and missing-value policies come from the audited configuration.</p>
      <div className="diagnostic-summary"><div><span>Tracts</span><strong>{display(vulnerability.tract_count)}</strong></div><div><span>Priority tracts</span><strong>{display(vulnerability.priority_tracts)}</strong></div><div><span>High-correlation pairs</span><strong>{rows(vulnerabilityCorrelation.high_pairs).length}</strong></div><div><span>Rank variation</span><strong>{display(vulnerabilitySensitivity.mean_rank_standard_deviation)}</strong></div></div>
      <details className="analysis-disclosure"><summary>Correlation and PCA diagnostics</summary><h4>Feature correlation</h4><p className="muted">Spearman correlation after configured missing-value handling and vulnerability-direction alignment. Red is negative; green is positive.</p><CorrelationHeatmap data={vulnerabilityCorrelation} />
        {Object.keys(diagnosticCorrelation).length > 0 && <><h4>Vulnerability, temperature, and built environment</h4><p className="muted">Pairwise-complete diagnostic correlations use available tract values without imputing optional NDVI, NDBI, or albedo. Heat exposure is the temperature variable. These relationships do not change the vulnerability score or optimization.</p><CorrelationHeatmap data={diagnosticCorrelation} />{rows(diagnosticCorrelation.strongest_pairs).length > 0 && <DataTable data={rows(diagnosticCorrelation.strongest_pairs)} />}</>}
        <PcaSummary data={vulnerabilityPca} />
      </details>
      <details className="analysis-disclosure"><summary>Missingness, collinearity, and weight-scheme details</summary><h4>Missing-value audit</h4><p className="muted">Median replaces a missing value with the study-area median and records the affected GEOID. No fill value is shown when nothing was missing.</p><DataTable data={rows(vulnerability.missingness)} /><h4>Variance inflation factors</h4><p className="muted">VIF near 1 indicates little linear redundancy; review is flagged at 5 or above.</p><DataTable data={rows(vulnerability.vif)} /><h4>Weight sensitivity</h4><p className="muted">Rank correlation near 1 means tract ordering is stable. Priority-tract Jaccard near 1 means the same tracts remain in the priority set.</p><DataTable data={rows(vulnerabilitySensitivity.comparisons)} /><h4>Weight schemes</h4><DataTable data={Object.entries(record(vulnerabilitySensitivity.schemes)).map(([scheme, weights]) => ({ scheme, meaning: schemeDescriptions[scheme], weights }))} /></details>
    </section>}
    {experiments.length > 0 && <section><h3>Sensitivity experiments</h3><p className="muted">{display(sensitivity.experiment_count ?? experiments.length)} deterministic solutions · {display(sensitivity.mobility_scenario)} · router {display(sensitivity.router)}</p><DataTable data={experiments} /></section>}
    {Object.keys(optimization).length > 0 && <section className="analysis-section--optimization"><h3>Optimization result</h3><p className="muted">Selected facilities and outcome metrics under the requested facility budget, travel threshold, and value profile.</p><DataTable data={[optimization]} /></section>}
    {showValidation && <section><h3>Constraint validation</h3><p className="muted">Checks only explicit hard constraints such as regional minimums and reports conflicts before optimization.</p><DataTable data={[validation]} /></section>}
    {Object.keys(routing).length > 0 && <section className="analysis-section--travel"><h3>Travel matrix analysis</h3><p className="muted">Describes how demand-to-candidate travel times were built, cached, and checked. It is input quality evidence, not a second site recommendation.</p><DataTable data={[routing]} /></section>}
    {(Object.keys(facilities).length > 0 || Object.keys(threshold).length > 0) && <section><h3>Coverage diagnostics</h3>
      {Object.keys(facilities).length > 0 && <><h4>Fixed time: minimum facilities</h4><DataTable data={[{ threshold_minutes: facilities.threshold_minutes, minimum_facilities: facilities.minimum_facilities, feasible_facility_count: facilities.feasible_facility_count, minimum_proven: facilities.minimum_proven, solver_status: facilities.solver_status }]} /></>}
      {Object.keys(threshold).length > 0 && <><h4>Fixed budget: minimum travel time</h4><DataTable data={[{ facility_budget: threshold.n_facilities, threshold_minutes: threshold.minimum_threshold_minutes, feasible: threshold.feasible, minimum_proven: threshold.feasible === true && threshold.search_complete === true, search_status: record(threshold.search).status }]} /></>}
    </section>}
    {Object.keys(alternative).length > 0 && <section><h3>Alternative coverage plan</h3><p>{alternative.accuracy === "approximate_haversine" ? "Approximate travel estimate; not verified by ORS. " : ""}This plan requires a longer threshold and does not meet the original time requirement.</p><DataTable data={[{ threshold_minutes: alternative.threshold_minutes, minimum_facilities: alternative.minimum_facilities, feasible_facility_count: alternative.feasible_facility_count, minimum_proven: alternative.minimum_proven }]} /><details><summary>Selected facilities</summary><p>{rows(alternative.selected_sites).map((site) => display(site.name)).join(", ")}</p></details></section>}
    {Object.keys(extension).length > 0 && <section><h3>Hypothetical sites: post-paper extension</h3><p>Approximate Haversine travel; no ORS coverage guarantee. The minimum applies to the finite expanded candidate set. Hypothetical sites require land, access, and suitability review.</p><DataTable data={[{ threshold_minutes: extension.threshold_minutes, minimum_total_facilities: extension.minimum_facilities, feasible_facility_count: extension.feasible_facility_count, existing_sites: extension.existing_selected_count, hypothetical_sites: extension.hypothetical_selected_count, minimum_proven: extension.minimum_proven, solver_status: extension.solver_status }]} />{extension.reason ? <p>{display(extension.reason)}</p> : null}<details><summary>Selected locations</summary><DataTable data={rows(extension.selected_sites)} /></details></section>}
    {Object.keys(approximateThreshold).length > 0 && <section><h3>Approximate complementary bound</h3><p className="warning-text">This Haversine screening estimate is retained because the precise ORS matrix is sparse outside the confirmed threshold. It is not a precise network result.</p><DataTable data={[approximateThreshold]} /></section>}
    {Object.keys(reachability).length > 0 && <section><h3>Reachability</h3><p className="muted">Tracts are ranked by travel time to their nearest current candidate facility. Larger values are harder to reach; an empty nearest time means structurally unreachable in the computed matrix.</p><DataTable data={[reachabilityOverview]} />{rows(reachability.hardest_demand).length > 0 && <><h4>Hardest tracts</h4>{onShowTract && <button type="button" className="button--secondary" onClick={() => onShowTract(String(rows(reachability.hardest_demand)[0].GEOID ?? rows(reachability.hardest_demand)[0].demand_id).split(":", 1)[0])}>Show hardest tract on map</button>}<DataTable data={rows(reachability.hardest_demand)} /></>}</section>}
    {resultQueries.map((answer, index) => {
      const topRows = rows(answer.top_rows);
      const computation = record(answer.computation);
      return topRows.length > 0 && <section key={`${display(answer.metric)}-${index}`}><h3>Requested result: {display(answer.metric_label)}</h3><p>{display(answer.method)}</p><p><strong>Formula:</strong> <code>{display(computation.formula)}</code></p><p className="muted"><strong>Data basis:</strong> {display(computation.data_basis)}</p>{onShowTract && <button type="button" className="button--secondary" onClick={() => onShowTract(String(topRows[0].GEOID))}>Show first-ranked tract on map</button>}<DataTable data={topRows} /></section>;
    })}
    {topTracts.length > 0 && <section><h3>Highest heat-exposure tracts</h3><DataTable data={topTracts} /></section>}
    {heatUrl && <section><h3>Heat distribution</h3><img className="result-image" src={heatUrl} alt="Heat distribution and spatial autocorrelation" /></section>}
    {Object.keys(scenario).length > 0 && <section><h3>Within-run scenario comparison</h3><DataTable data={rows(scenario.scenarios)} />{rows(scenario.comparisons).length > 0 && <><h4>Changes from baseline</h4><DataTable data={rows(scenario.comparisons)} /></>}</section>}
    {failures.length > 0 && <section><h3>Tool errors</h3><DataTable data={failures} /></section>}
  </div>;
}

export function ProvenanceView({ result }: { result: RunResult }) {
  const execution = result.execution;
  const status = useQuery({ queryKey: ["data-source-status"], queryFn: getDataSourceStatus,
    staleTime: 24 * 60 * 60 * 1000, retry: false });
  const official = useQuery({ queryKey: ["official-resources", result.task.place_name],
    queryFn: () => getOfficialResources(result.task.place_name),
    staleTime: 24 * 60 * 60 * 1000, retry: false });
  return <div className="detail-stack">
    <section><h3>Execution provenance</h3><DataTable data={[{ ...execution.parameters, ...execution.cache_stats, mode: execution.mode, phase: execution.phase, accuracy: execution.accuracy, router_requested: execution.router_requested, router_used: execution.router_used, status: execution.status }]} /></section>
    <section><h3>Data lineage</h3>{result.lineage?.length ? <DataTable data={result.lineage} collapsedColumns={["notes"]} /> : <p className="muted">No lineage records were published.</p>}</section>
    <section><h3>Approved source monitor</h3><p className="muted">Read-only checks of configured public providers. Updates are reported for review and never installed automatically.</p>
      {status.isPending && <p className="source-monitor-loading">Checking official catalogs and service availability<span className="animated-ellipsis" aria-label="in progress" /></p>}
      {status.isError && <p className="source-monitor-error">The approved-source check could not be completed. Current run provenance is unchanged.</p>}
      {status.data && <><p><span className={`status ${status.data.updates_available ? "status--warning" : ""}`}>{status.data.updates_available ? "Review update" : "No confirmed update"}</span> <span className="muted">Checked {new Date(status.data.checked_at).toLocaleString()}</span></p><DataTable data={status.data.sources} />{(status.data.compatibility_warnings ?? []).map((warning) => <p className="source-monitor-warning" key={warning}>{warning}</p>)}</>}
    </section>
    {(official.isPending || official.isError || Boolean(official.data && official.data.status !== "not_applicable")) && <section><h3>Official cooling resources</h3>
      {official.isPending && <p className="source-monitor-loading">Checking city or county official catalogs<span className="animated-ellipsis" aria-label="in progress" /></p>}
      {official.isError && <p className="source-monitor-error">The city or county catalog check could not be completed.</p>}
      {official.data && <><p className="muted">{official.data.message} Catalog matches are for verification and never mark OpenStreetMap candidates as active cooling centers.</p>{(official.data.resources ?? []).length ? <DataTable data={official.data.resources ?? []} /> : null}</>}
    </section>}
  </div>;
}
