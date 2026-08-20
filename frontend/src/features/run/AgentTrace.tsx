import type { PublicAgentEvent } from "../../api/models";
import { useState } from "react";
import { useMediaQuery } from "../../hooks/useMediaQuery";

interface AgentTraceProps {
  events: PublicAgentEvent[];
}

const TOOL_LABELS: Record<string, string> = {
  resolve_study_area: "Resolve study area",
  prepare_demand: "Prepare population and exposure data",
  fetch_candidate_sites: "Find candidate facilities",
  build_travel_matrix: "Calculate network travel times",
  optimize_sites: "Optimize facility locations",
  generate_outputs: "Generate map and report",
};
const readable = (value: string) => TOOL_LABELS[value] ?? value.replaceAll("_", " ");

export function AgentTrace({ events }: AgentTraceProps) {
  const mobile = useMediaQuery("(max-width: 620px)");
  const [open, setOpen] = useState(false);
  return (
    <details className="panel trace-panel mobile-trace-disclosure" open={!mobile || open} onToggle={(event) => {
      if (mobile) setOpen(event.currentTarget.open);
    }}>
      <summary>
      <div className="section-heading">
        <div><p className="eyebrow">Process</p><h2>Run activity</h2></div>
        <span className="status">{events.length} steps</span>
      </div>
      </summary>
      <div className="mobile-trace-body">
      {events.length === 0 ? (
        <p className="muted">Observable decisions will appear after execution begins.</p>
      ) : (
        <ol className="trace-list">
          {events.map((event, index) => (
            <li key={`${event.seq ?? index}-${event.stage}-${event.tool ?? "none"}`}>
              <span className={`trace-dot trace-dot--${event.stage}`} />
              <div>
                <span className="trace-meta">{readable(event.stage)}{event.tool ? ` · ${readable(event.tool)}` : ""}</span>
                <p>{event.message}</p>
              </div>
            </li>
          ))}
        </ol>
      )}
      </div>
    </details>
  );
}
