import { useEffect, useState } from "react";
import type { RunProgress as Progress, RunSnapshot } from "../../api/models";

interface RunProgressProps {
  status: RunSnapshot["status"];
  phase: string | null | undefined;
  progress: Progress | null | undefined;
}

export function RunProgress({ status, phase, progress }: RunProgressProps) {
  const active = status === "queued" || status === "running";
  const complete = status === "succeeded";
  const visibleProgress = active ? progress : null;
  const timing = visibleProgress as (Progress & {
    stage_elapsed_seconds?: number;
    last_activity_seconds?: number;
  }) | null;
  const percent = complete ? 100 : Math.round((visibleProgress?.fraction ?? 0) * 100);
  const [eta, setEta] = useState(visibleProgress?.eta_seconds ?? null);
  useEffect(() => {
    setEta(visibleProgress?.eta_seconds ?? null);
    if (!active || visibleProgress?.eta_seconds == null) return;
    const started = Date.now();
    const initial = visibleProgress.eta_seconds;
    const timer = window.setInterval(() => setEta(Math.max(0, initial - (Date.now() - started) / 1000)), 1000);
    return () => window.clearInterval(timer);
  }, [active, visibleProgress?.eta_seconds]);
  return (
    <section className="run-progress" aria-live="polite">
      <div className="run-progress__labels">
        <div>
          <strong>{complete ? "Run complete" : visibleProgress?.label ?? status.replaceAll("_", " ")}{active && <span className="animated-ellipsis" aria-label="in progress" />}</strong>
          <span>{phase ? `${phase} phase` : "Preparing execution"}</span>
        </div>
        <span>{complete ? "100%" : visibleProgress ? `${percent}%` : status}</span>
      </div>
      <div className={`progress-track${active ? " is-active" : ""}`} role="progressbar" aria-valuenow={percent} aria-valuemin={0} aria-valuemax={100}>
        <span className="progress-track__value" style={{ width: `${percent}%` }} />
        {active && <i className="progress-track__braid" aria-hidden="true" />}
      </div>
      {visibleProgress && (
        <div className="progress-stats">
          <span>{visibleProgress.done}/{visibleProgress.total} {visibleProgress.unit}</span>
          <span>{visibleProgress.cache_hits} cache hits</span>
          <span>{visibleProgress.remote_pairs} remote pairs</span>
          {timing?.stage_elapsed_seconds != null && <span>Stage elapsed {Math.floor(timing.stage_elapsed_seconds)}s</span>}
          {timing?.last_activity_seconds != null && timing.last_activity_seconds >= 1
            && <span>Last activity {Math.floor(timing.last_activity_seconds)}s ago</span>}
          {eta != null && (eta <= 0 && visibleProgress.done < visibleProgress.total
            ? <span>Finishing the current solver setting</span>
            : <span>ETA ≈ {Math.ceil(eta)}s</span>)}
        </div>
      )}
    </section>
  );
}
