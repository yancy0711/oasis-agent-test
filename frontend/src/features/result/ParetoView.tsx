import { useMemo, useState } from "react";
import type { RunResult } from "../../api/models";
import { useReviseRun } from "../run/run.mutations";

export interface ParetoPoint {
  alpha: number; efficiency: number; equality: number; gini: number | null;
  covered_population_ratio: number; vulnerable_coverage_ratio: number;
  mean_access_score: number; mean_travel_minutes: number;
  selected_ids: string[]; is_frontier: boolean;
  selected_sites: Array<{ id: string; name: string; longitude: number; latitude: number }>;
}

interface VisualGroup { indices: number[]; point: ParetoPoint }

const percent = (value: number) => `${(value * 100).toFixed(1)}%`;
const label = (alpha: number) => alpha <= .2 ? "Efficiency emphasis" : alpha >= .8 ? "Equality emphasis" : "Balanced trade-off";
const validPoint = (value: Record<string, unknown>) => typeof value.alpha === "number"
  && typeof value.efficiency === "number" && typeof value.gini === "number"
  && Array.isArray(value.selected_ids);
const sameMetrics = (left: ParetoPoint, right: ParetoPoint) => Math.abs(left.efficiency - right.efficiency) < .00005
  && Math.abs((left.gini ?? 0) - (right.gini ?? 0)) < .00005;

export function ParetoView({ runId, result, url, onPreview }: { runId: string; result: RunResult; url?: string; onPreview?: (point: ParetoPoint | null) => void }) {
  const points = useMemo(() => (result.pareto_points ?? []).filter(validPoint) as unknown as ParetoPoint[], [result.pareto_points]);
  const initial = Math.max(0, points.findIndex((point) => Math.abs(point.alpha - (result.task.equity_alpha ?? 0)) < .001));
  const [selectedIndex, setSelectedIndex] = useState(initial);
  const [hoveredGroup, setHoveredGroup] = useState<number | null>(null);
  const revise = useReviseRun(runId);
  if (!points.length) return url ? <img className="result-image" src={url} alt="Efficiency and equity Pareto frontier" /> : <p className="empty-tab">The Pareto frontier is not available for this result.</p>;

  const groups = points.reduce<VisualGroup[]>((resultGroups, point, index) => {
    const existing = resultGroups.find((group) => sameMetrics(group.point, point));
    if (existing) existing.indices.push(index);
    else resultGroups.push({ indices: [index], point });
    return resultGroups;
  }, []);
  const selected = points[Math.min(selectedIndex, points.length - 1)];
  const selectedGroupIndex = Math.max(0, groups.findIndex((group) => group.indices.includes(selectedIndex)));
  const selectedGroup = groups[selectedGroupIndex];
  const efficiencies = points.map((point) => point.efficiency);
  const ginis = points.map((point) => point.gini ?? 0);
  const xMin = Math.min(...efficiencies), xMax = Math.max(...efficiencies);
  const yMin = Math.min(...ginis), yMax = Math.max(...ginis);
  const sameX = Math.abs(xMax - xMin) < .0005;
  const sameY = Math.abs(yMax - yMin) < .0005;
  const x = (value: number) => 70 + (value - xMin) / Math.max(xMax - xMin, .001) * 550;
  const y = (value: number) => 270 - (value - yMin) / Math.max(yMax - yMin, .001) * 220;
  const positions = groups.map((group) => ({ x: x(group.point.efficiency), y: y(group.point.gini ?? 0) }));
  const currentAlpha = result.task.equity_alpha;
  const distances = points.map((point, index) => {
    if (!index || index === points.length - 1) return Number.NEGATIVE_INFINITY;
    const a = points[0], b = points[points.length - 1];
    const spanX = Math.max(Math.abs(b.efficiency - a.efficiency), .001);
    const spanY = Math.max(Math.abs((b.gini ?? 0) - (a.gini ?? 0)), .001);
    const line = Math.abs(((b.gini ?? 0) - (a.gini ?? 0)) * point.efficiency
      - (b.efficiency - a.efficiency) * (point.gini ?? 0)
      + b.efficiency * (a.gini ?? 0) - (b.gini ?? 0) * a.efficiency);
    return line / Math.hypot((b.gini ?? 0) - (a.gini ?? 0), b.efficiency - a.efficiency)
      / Math.hypot(spanX, spanY);
  });
  const kneeDistance = Math.max(...distances);
  const kneeIndex = Number.isFinite(kneeDistance) ? distances.indexOf(kneeDistance) : -1;
  const apply = () => revise.mutate({ task: { ...result.task, equity_alpha: selected.alpha, coverage_target: null } });
  const alphaRange = (group: VisualGroup) => {
    const values = group.indices.map((index) => points[index].alpha);
    return values.length === 1 ? `α = ${values[0].toFixed(1)}` : `α = ${Math.min(...values).toFixed(1)}–${Math.max(...values).toFixed(1)}`;
  };

  return <div className="pareto-interactive">
    <div><h3>Efficiency–Gini solution explorer</h3><p className="muted">Select a point to inspect it. Lower Gini means more equal access; higher gradual access means better population-weighted accessibility. The yellow point is the point being inspected, not an automatically best solution. Inspection does not change the recommendation until you confirm it.</p></div>
    <div className="pareto-layout"><svg viewBox="0 0 680 320" role="img" aria-label="Interactive efficiency and Gini frontier">
      <line x1="54" y1="276" x2="640" y2="276" /><line x1="54" y1="276" x2="54" y2="32" />
      <text x="345" y="312">Population-weighted gradual access (higher is better) →</text>
      <text transform="translate(17 160) rotate(-90)">Access Gini (lower is more equal) →</text>
      <text x={sameX ? 345 : 70} y="294">{percent(xMin)}</text>{!sameX && <text x="620" y="294">{percent(xMax)}</text>}
      <text x="34" y={sameY ? 164 : 274}>{yMin.toFixed(3)}</text>{!sameY && <text x="34" y="54">{yMax.toFixed(3)}</text>}
      {groups.slice(1).map((group, index) => <line className="pareto-path" key={`line-${group.point.alpha}`} x1={positions[index].x} y1={positions[index].y} x2={positions[index + 1].x} y2={positions[index + 1].y} />)}
      {groups.map((group, groupIndex) => {
        const representative = group.indices.includes(initial) ? initial : group.indices[0];
        const description = group.indices.length > 1 ? `${group.indices.length} alpha settings with identical rounded metrics` : alphaRange(group);
        return <g key={`${group.point.efficiency}-${group.point.gini}`} onMouseEnter={() => setHoveredGroup(groupIndex)} onMouseLeave={() => setHoveredGroup(null)} onClick={() => setSelectedIndex(representative)} role="button" tabIndex={0} onFocus={() => setHoveredGroup(groupIndex)} onBlur={() => setHoveredGroup(null)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") setSelectedIndex(representative); }} aria-label={`${description}, access ${percent(group.point.efficiency)}, Gini ${group.point.gini?.toFixed(3)}`}>
          <circle className={`${group.indices.some((index) => points[index].is_frontier) ? "is-frontier" : ""} ${group.indices.includes(selectedIndex) ? "is-selected" : ""} ${group.indices.includes(kneeIndex) ? "is-knee" : ""}`} cx={positions[groupIndex].x} cy={positions[groupIndex].y} r={group.indices.includes(selectedIndex) ? 8 : 6}><title>{`${alphaRange(group)} · gradual access ${percent(group.point.efficiency)} · Gini ${group.point.gini?.toFixed(3)}`}</title></circle>
        </g>;
      })}
      {hoveredGroup != null && <g className="pareto-hover" transform={`translate(${Math.min(430, Math.max(80, positions[hoveredGroup].x + 12))} ${Math.max(34, positions[hoveredGroup].y - 12)})`}><rect width="220" height="30" rx="6" /><text x="110" y="20">{`${alphaRange(groups[hoveredGroup])} · Access ${percent(groups[hoveredGroup].point.efficiency)} · Gini ${groups[hoveredGroup].point.gini?.toFixed(3)}`}</text></g>}
    </svg><article className="pareto-detail"><div className="pareto-tags"><span>{alphaRange(selectedGroup)}</span><span>{label(selected.alpha)}</span>{currentAlpha == null && <span>Inspection only</span>}{selectedGroup.indices.includes(kneeIndex) && <span>Balance reference</span>}{selected.is_frontier && <span>Non-dominated</span>}{currentAlpha != null && selectedGroup.indices.some((index) => Math.abs(points[index].alpha - currentAlpha) < .001) && <span>Current result</span>}</div>
      {selectedGroup.indices.length > 1 && <p className="pareto-overlap-note">{selectedGroup.indices.length} α settings have the same rounded metrics, so they are represented by one point.</p>}
      <dl><div><dt>Gradual access</dt><dd>{percent(selected.efficiency)}</dd></div><div><dt>Access Gini</dt><dd>{selected.gini?.toFixed(3) ?? "Undefined"}</dd></div><div><dt>Equality, 1 − Gini</dt><dd>{percent(selected.equality)}</dd></div><div><dt>Binary population coverage</dt><dd>{percent(selected.covered_population_ratio)}</dd></div><div><dt>Vulnerable population coverage</dt><dd>{percent(selected.vulnerable_coverage_ratio)}</dd></div><div><dt>Mean assigned travel</dt><dd>{selected.mean_travel_minutes.toFixed(1)} min</dd></div></dl>
      <details className="pareto-method"><summary>How to read α and these metrics</summary>
        <p><i>J</i><sub>α</sub> = (1 − α) · population-weighted gradual access + α · (1 − access Gini).</p>
        <p>α = 0 emphasizes total access; α = 1 emphasizes equal access. Each α guides iterative reweighting toward underserved tracts, and the expression above scores the candidate rounds. It is a transparent Gini-aware search, not a claim that Gini is solved as one exact linear term.</p>
        <p>α changes the trade-off used to select sites, but it does not guarantee a low Gini when the budget, threshold, candidates, and travel network leave few equitable alternatives.</p>
        <p>Binary coverage counts people within the threshold. Gradual access additionally discounts access as travel time approaches that threshold. Vulnerable coverage applies the binary test to the configured priority population. Mean assigned travel is calculated only for assigned demand.</p>
      </details>
      <details><summary>{selected.selected_ids.length} selected candidate facilities</summary><p>{selected.selected_sites?.map((site) => site.name).join(", ") || selected.selected_ids.join(", ")}</p></details>
      <button type="button" className="button--secondary" onClick={() => onPreview?.(selected)}>Preview these sites on map</button>
      <button type="button" disabled={revise.isPending || (currentAlpha != null && Math.abs(currentAlpha - selected.alpha) < .001)} onClick={apply}>{revise.isPending ? "Creating revision..." : "Use this alpha in a new revision"}</button>
      {revise.isError && <p className="error">{revise.error instanceof Error ? revise.error.message : "Could not create the revision."}</p>}
    </article></div>
  </div>;
}
