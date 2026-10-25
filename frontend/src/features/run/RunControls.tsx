import { useCancelRun } from "./run.mutations";
import { LoadingText } from "../../components/LoadingText";

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
      {cancelMutation.isPending ? <LoadingText>Cancelling</LoadingText> : "Cancel run"}
    </button>
  );
}
