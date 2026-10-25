import type { RunSnapshot } from "../../api/models";
import { agentMessage, isPreview, isTerminalRun, visibleResult } from "../run/run.selectors";
import { MetricsView } from "./MetricsView";
import { RecoveryPanel } from "./RecoveryPanel";
import { ResultTabs } from "./ResultTabs";
import { ExecutionSummary } from "./ResultShared";
import { useReviseRun } from "../run/run.mutations";

export function ResultPanel({ snapshot }: { snapshot: RunSnapshot }) {
  const retry = useReviseRun(snapshot.run_id);
  const result = visibleResult(snapshot);
  const message = agentMessage(snapshot);
  const approximateFallback = Boolean(
    snapshot.error && result?.execution.accuracy === "approximate"
  );
  return (
    <section className="panel result-panel">
      <div className="section-heading">
        <div><p className="eyebrow">Decision support</p><h2>Result</h2></div>
        {(isPreview(snapshot) || approximateFallback) && <span className="status status--warning">{approximateFallback ? "Approximate fallback" : "Approximate preview"}</span>}
      </div>
      {!result ? (
        isTerminalRun(snapshot) ? (
          <div className="error error--block" role="alert">
            <strong>{snapshot.status === "interrupted" ? "Run interrupted" : "No result was produced"}</strong>
            <p>{message ?? "The run ended before a map or report was created."}</p>
            <p>Return to the confirmed task and run it again. Completed provider downloads remain available in the durable cache.</p>
          </div>
        ) : <p className="muted">The result panel will update from the same run snapshot as progress and trace<span className="animated-ellipsis" aria-label="in progress" /></p>
      ) : (
        <>
          <div className="run-context" aria-label="Current decision context">
            <span><b>Place</b>{result.task.place_name}</span>
            <span><b>Facilities</b>{result.task.n_facilities}</span>
            <span><b>Threshold</b>{result.task.threshold_minutes} min</span>
            <span><b>Mobility</b>{result.task.mobility_scenario}</span>
            <span><b>Equity setting</b>{result.task.equity_alpha == null ? "Not applied" : `α = ${result.task.equity_alpha}`}</span>
          </div>
          {message && !result.recovery && <p className={`agent-message${approximateFallback ? " agent-message--warning" : ""}`}>{message}</p>}
          {(!result.recovery || result.execution.router_used) && <ExecutionSummary result={result} />}
          <RecoveryPanel result={result} onRetry={() => retry.mutate({})} retryPending={retry.isPending} retryError={retry.error?.message} />
          <MetricsView result={result} />
          <ResultTabs runId={snapshot.run_id} result={result} />
        </>
      )}
    </section>
  );
}
