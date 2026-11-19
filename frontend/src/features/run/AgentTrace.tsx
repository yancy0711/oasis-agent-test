import { useWorkspacePreferences } from "../../app/WorkspacePreferences";
import type { PublicAgentEvent } from "../../api/models";
import { useEffect, useState } from "react";
import { useMediaQuery } from "../../hooks/useMediaQuery";

interface AgentTraceProps {
  events: PublicAgentEvent[];
  compact?: boolean;
  startedAt?: string;
  finishedAt?: string;
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

export function AgentTrace({ events, compact, startedAt, finishedAt }: AgentTraceProps) {
  const { t } = useWorkspacePreferences();
  const mobile = useMediaQuery("(max-width: 620px)");
  const [open, setOpen] = useState(false);
  const [now, setNow] = useState(Date.now);
  const start = Date.parse(startedAt ?? "");
  const elapsed = (timestamp: number) => Number.isFinite(start) && Number.isFinite(timestamp) ? Math.max(0, Math.floor((timestamp - start) / 1000)) : null;
  const seconds = elapsed(finishedAt ? Date.parse(finishedAt) : now);
  useEffect(() => {
    if (!startedAt || finishedAt) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [startedAt, finishedAt]);
  return (
    <details className="panel trace-panel mobile-trace-disclosure" open={compact ? open : !mobile || open} onToggle={(event) => {
      if (compact || mobile) setOpen(event.currentTarget.open);
    }}>
      <summary>
      <div className="section-heading">
        <h2>{t("Run activity")}</h2>
        <span>{events.length} {events.length === 1 ? "step" : "steps"}{seconds !== null && ` · ${seconds}s`}</span>
      </div>
      </summary>
      <div className="mobile-trace-body">
      {events.length === 0 ? (
        <p className="muted">Observable decisions will appear after execution begins.</p>
      ) : (
        <ol className="trace-list">
          {events.map((event, index) => {
            const eventTime = elapsed(Date.parse(event.created_at ?? ""));
            return (
            <li key={`${event.seq ?? index}-${event.stage}-${event.tool ?? "none"}`}>
              <span className={`trace-dot trace-dot--${event.stage}`} />
              <div>
                <span className="trace-meta">{readable(event.stage)}{event.tool ? ` · ${readable(event.tool)}` : ""}{eventTime !== null && ` · +${eventTime}s`}</span>
                <p>{event.message}</p>
              </div>
            </li>
          ); })}
        </ol>
      )}
      </div>
    </details>
  );
}
