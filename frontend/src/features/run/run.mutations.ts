import { useMutation, useQueryClient } from "@tanstack/react-query";
import { cancelRun, reviseRun, startRun } from "../../api/client";
import type { RoutingMode, RunRevisionRequest, TaskSpec } from "../../api/models";
import { runKeys } from "../../api/queryKeys";
import { useAppStore } from "../../store/appStore";
import type { RunSnapshot } from "../../api/models";
import { TERMINAL_RUN_STATUSES } from "./run.constants";

export function useStartRun() {
  const queryClient = useQueryClient();
  const setActiveRunId = useAppStore((state) => state.setActiveRunId);
  return useMutation({
    mutationFn: async (input: {
      task: TaskSpec;
      mode: RoutingMode;
      candidateUploadId: string | null;
    }) => {
      const activeRunId = useAppStore.getState().activeRunId;
      const active = activeRunId
        ? queryClient.getQueryData<RunSnapshot>(runKeys.detail(activeRunId))
        : undefined;
      if (activeRunId && active && !TERMINAL_RUN_STATUSES.has(active.status)) {
        return reviseRun(activeRunId, {
          task: input.task,
          mode: input.mode,
          candidate_upload_id: input.candidateUploadId,
          llm_session_id: useAppStore.getState().llmSessionId,
        });
      }
      return startRun(input.task, input.mode, input.candidateUploadId, useAppStore.getState().llmSessionId);
    },
    onSuccess: (accepted) => {
      setActiveRunId(accepted.run_id);
      if (accepted.parent_run_id) {
        queryClient.invalidateQueries({ queryKey: runKeys.detail(accepted.parent_run_id) });
      }
      queryClient.invalidateQueries({ queryKey: runKeys.detail(accepted.run_id) });
    },
  });
}

export function useCancelRun(runId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => cancelRun(runId),
    onSuccess: (snapshot) => {
      queryClient.setQueryData(runKeys.detail(runId), snapshot);
      if (!TERMINAL_RUN_STATUSES.has(snapshot.status)) {
        queryClient.invalidateQueries({ queryKey: runKeys.detail(runId) });
      }
    },
  });
}

export function useReviseRun(runId: string) {
  const queryClient = useQueryClient();
  const setActiveRunId = useAppStore((state) => state.setActiveRunId);
  return useMutation({
    mutationFn: (body: RunRevisionRequest) => reviseRun(runId, {
      ...body, llm_session_id: useAppStore.getState().llmSessionId,
    }),
    onSuccess: (accepted) => {
      setActiveRunId(accepted.run_id);
      queryClient.invalidateQueries({ queryKey: runKeys.detail(runId) });
      queryClient.invalidateQueries({ queryKey: runKeys.detail(accepted.run_id) });
    },
  });
}
