import { useCancelRun } from "./run.mutations";
import { LoadingText } from "../../components/LoadingText";
import { useWorkspacePreferences } from "../../app/WorkspacePreferences";

interface RunControlsProps {
  runId: string;
  terminal: boolean;
}

export function RunControls({ runId, terminal }: RunControlsProps) {
  const cancelMutation = useCancelRun(runId);
  const { t } = useWorkspacePreferences();
  if (terminal) return null;
  return (
    <button
      className="button button--danger"
      type="button"
      disabled={cancelMutation.isPending}
      onClick={() => cancelMutation.mutate()}
    >
      {cancelMutation.isPending ? <LoadingText>{t("Cancelling")}</LoadingText> : t("Cancel run")}
    </button>
  );
}
