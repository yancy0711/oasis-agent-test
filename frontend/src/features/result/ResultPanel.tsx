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
  const savedMessage = agentMessage(snapshot);
  const diagnostic = result && ["minimum_facilities", "minimum_threshold"].includes(result.task.analysis_goal);
  const fixedInputs = result?.task.diagnostic_fixed_inputs ?? (result?.task.analysis_goal === "minimum_facilities" ? ["threshold_minutes"] : ["n_facilities"]);
  const approximateFallback = Boolean(
    snapshot.error && result?.execution.accuracy === "approximate"
  );
  const refinement = result?.diagnostics?.precise_refinement as Record<string, unknown> | undefined;
  const facilities = result?.diagnostics?.minimum_facilities as Record<string, unknown> | undefined;
  const threshold = result?.diagnostics?.minimum_threshold as Record<string, unknown> | undefined;
  const calculatedCount = facilities?.feasible ? facilities.minimum_facilities ?? facilities.feasible_facility_count : null;
  const calculatedTime = threshold?.feasible ? threshold.minimum_threshold_minutes : null;
  const alternative = result?.diagnostics?.coverage_alternative as Record<string, unknown> | undefined;
  // Older saved fallbacks replaced the answer with a provider warning. Their
  // structured diagnostics still contain the computed values.
  let message = savedMessage;
  if (refinement?.status === "unavailable" && savedMessage?.startsWith("The full study-area ORS matrix was unavailable")) {
    message = typeof calculatedCount === "number" ? `Estimated ${facilities?.minimum_proven ? "minimum" : "feasible plan"}: ${calculatedCount} facilities within ${facilities?.threshold_minutes} minutes.`
      : typeof calculatedTime === "number" ? `Estimated travel threshold: ${Number(calculatedTime.toFixed(1))} minutes with ${threshold?.n_facilities} facilities.`
        : "The approximate result is retained. Review the map and Analysis tab for the computed feasibility outcome.";
    if (facilities?.solver_status === "Infeasible") {
      message = `No plan using the current candidates meets the confirmed target within ${facilities.threshold_minutes} minutes in the approximate model.`;
      if (alternative?.feasible && typeof alternative.threshold_minutes === "number") {
        message += ` Alternative estimate: ${alternative.minimum_facilities ?? alternative.feasible_facility_count} facilities at ${Number(alternative.threshold_minutes.toFixed(1))} minutes.`;
      }
    }
  }
  return (
    <section className="panel result-panel">
      <div className="section-heading">
        <div><p className="eyebrow">Decision support</p><h2>Result</h2></div>
        {(isPreview(snapshot) || approximateFallback) && <span className="status status--warning">{approximateFallback ? "Approximate result" : "Approximate preview"}</span>}
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
            <span><b>Facilities</b>{diagnostic && !fixedInputs.includes("n_facilities") ? typeof calculatedCount === "number" ? calculatedCount : "To calculate" : result.task.n_facilities}</span>
            <span><b>Threshold</b>{diagnostic && !fixedInputs.includes("threshold_minutes") ? typeof calculatedTime === "number" ? `${Number(calculatedTime.toFixed(1))} min` : "To calculate" : `${result.task.threshold_minutes} min`}</span>
            <span><b>Mobility</b>{result.task.mobility_scenario}</span>
            <span><b>Equity setting</b>{result.task.equity_alpha == null ? "Not applied" : `α = ${result.task.equity_alpha}`}</span>
          </div>
          {message && !result.recovery && <p className="agent-message">{message}</p>}
          {refinement?.status === "unavailable" && <details className="execution-notice execution-notice--approximate"><summary>ORS refinement unavailable — showing the estimate</summary><p>{String(refinement.details ?? refinement.reason ?? "Network refinement could not be completed.")}</p>{refinement.next_steps ? <p>{String(refinement.next_steps)}</p> : null}</details>}
          {(!result.recovery || result.execution.router_used) && <ExecutionSummary result={result} />}
          <RecoveryPanel result={result} onRetry={() => retry.mutate({})} retryPending={retry.isPending} retryError={retry.error?.message} />
          <MetricsView result={result} />
          <ResultTabs runId={snapshot.run_id} result={result} />
        </>
      )}
    </section>
  );
}
