import { useCancelRun } from "./run.mutations";

interface RunControlsProps {
  runId: string;
  terminal: boolean;
}

export function RunControls({ runId, terminal }: RunControlsProps) {
  const cancelMutation = useCancelRun(runId);
  if (terminal) return null;
  return (
    <button
      className="button button--danger"
      type="button"
      disabled={cancelMutation.isPending}
      onClick={() => cancelMutation.mutate()}
    >
      {cancelMutation.isPending ? "Cancelling…" : "Cancel run"}
    </button>
  );
}
