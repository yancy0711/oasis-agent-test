import { useAppStore } from "../../store/appStore";
import { LoadingText } from "../../components/LoadingText";
import { ResultPanel } from "../result/ResultPanel";
import { AgentTrace } from "./AgentTrace";
import { RunControls } from "./RunControls";
import { RunProgress } from "./RunProgress";
import { isTerminalRun } from "./run.selectors";
import { useRunLifecycle } from "./useRunLifecycle";
import { useRunPolling } from "./useRunPolling";

export function RunWorkspace() {
  const runId = useAppStore((state) => state.activeRunId);
  const query = useRunPolling(runId);
  useRunLifecycle(query.data);

  if (!runId) {
    return <section className="panel empty-state"><p>Confirm a task to start an auditable run.</p></section>;
  }
  if (query.isLoading) {
    return <section className="panel empty-state"><p><LoadingText>{`Loading run ${runId.slice(0, 8)}`}</LoadingText></p></section>;
  }
  if (query.isError || !query.data) {
    return <section className="panel"><p className="error error--block">{query.error?.message ?? "Run unavailable."}</p></section>;
  }

  const snapshot = query.data;
  return (
    <div className="run-workspace">
      <section className="panel run-summary">
        <div className="section-heading">
          <div><p className="eyebrow">Run {snapshot.run_id.slice(0, 8)}</p><h2>{snapshot.task.place_name}</h2></div>
          <div className="run-summary__actions">
            <span className={`status status--${snapshot.status}`}>{snapshot.status.replaceAll("_", " ")}</span>
            <RunControls runId={snapshot.run_id} terminal={isTerminalRun(snapshot)} />
          </div>
        </div>
        <RunProgress status={snapshot.status} phase={snapshot.phase} progress={snapshot.progress} />
      </section>

      <div className="workspace-grid">
        <AgentTrace events={snapshot.events ?? []} />
        <ResultPanel snapshot={snapshot} />
      </div>
    </div>
  );
}
