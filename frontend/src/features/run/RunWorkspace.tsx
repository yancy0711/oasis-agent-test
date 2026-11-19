import { useWorkspacePreferences } from "../../app/WorkspacePreferences";
import { useAppStore } from "../../store/appStore";
import { LoadingText } from "../../components/LoadingText";
import { ResultPanel } from "../result/ResultPanel";
import { AgentTrace } from "./AgentTrace";
import { RunControls } from "./RunControls";
import { RunProgress } from "./RunProgress";
import { isTerminalRun, visibleResult } from "./run.selectors";
import { useRunLifecycle } from "./useRunLifecycle";
import { useRunPolling } from "./useRunPolling";

type RunSlots = Record<"run" | "result" | "report" | "map" | "data" | "overlay", HTMLDivElement | null>;

export function RunWorkspace({ slots, onResult, onEditTask }: { slots?: RunSlots; onResult?: () => void; onEditTask?: () => void } = {}) {
  const { t } = useWorkspacePreferences();
  const runId = useAppStore((state) => state.activeRunId);
  const query = useRunPolling(runId);
  useRunLifecycle(query.data);
  const lastResult = useRef<string>(null);
  const resultKey = visibleResult(query.data) ? `${runId}:${query.data?.result ? "final" : "preview"}` : null;
  useEffect(() => {
    if (resultKey && resultKey !== lastResult.current) onResult?.();
    lastResult.current = resultKey;
  }, [resultKey, onResult]);

  function placeOutput(run: ReactNode, result?: ReactNode) {
    return slots ? <>
      {slots.run && createPortal(run, slots.run)}
      {slots.result && createPortal(result ?? run, slots.result)}
    </> : run;
  }

  if (!runId) {
    const empty = <p className="workbench-description">{t("Confirm and run a task to see its results.")}</p>;
    return slots ? placeOutput(null, empty) : empty;
  }
  if (query.isLoading) {
    return placeOutput(<section className="panel empty-state"><p><LoadingText>{`Loading run ${runId.slice(0, 8)}`}</LoadingText></p></section>);
  }
  if (!query.data) {
    return placeOutput(<section className="panel"><p className="error error--block">{query.error?.message ?? "Run unavailable."}</p><button type="button" onClick={() => void query.refetch()}>Retry connection</button></section>);
  }

  const snapshot = query.data;
  const terminal = isTerminalRun(snapshot);
  const runSummary = <>
    {query.isError && <div className="notice" role="alert"><p>Connection interrupted. Showing the last received result; the server's current state is unknown.</p><button type="button" className="button--secondary" onClick={() => void query.refetch()}>Retry connection</button></div>}
    <section className="panel run-summary">
      <div className="section-heading">
        <div><p className="eyebrow">Run {snapshot.run_id.slice(0, 8)}</p><h2>{snapshot.task.place_name}</h2></div>
        <div className="run-summary__actions">
          <span className={`status status--${snapshot.status}`}>{t(snapshot.status.replaceAll("_", " "))}</span>
          <RunControls runId={snapshot.run_id} terminal={terminal} />
        </div>
      </div>
      <RunProgress status={snapshot.status} phase={snapshot.phase} progress={snapshot.progress} />
      {slots && terminal && !resultKey && snapshot.error && <p className="error error--block" role="alert">{snapshot.error}</p>}
    </section>
  </>;
  const resultPanel = <ResultPanel key={snapshot.run_id} snapshot={snapshot} workspace={slots} onEditTask={onEditTask} />;
  if (slots) return placeOutput(<div className="run-workspace">{runSummary}<AgentTrace events={snapshot.events ?? []} compact startedAt={snapshot.created_at} finishedAt={terminal ? snapshot.updated_at : undefined} /></div>, resultPanel);
  return (
    <div className="run-workspace">
      {runSummary}
      <div className="workspace-grid">
        <AgentTrace events={snapshot.events ?? []} />
        {resultPanel}
      </div>
    </div>
  );
}
import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
