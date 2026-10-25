import { useMutation, useQueryClient } from "@tanstack/react-query";
import { cancelRun, getRun, reviseRun, startRun } from "../../api/client";
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
      const { activeRunId, taskVersion } = useAppStore.getState();
      const active = activeRunId
        ? queryClient.getQueryData<RunSnapshot>(runKeys.detail(activeRunId)) ?? await getRun(activeRunId)
        : undefined;
      if (taskVersion !== useAppStore.getState().taskVersion) return null;
      const accepted = activeRunId && active && !TERMINAL_RUN_STATUSES.has(active.status)
        ? await reviseRun(activeRunId, {
          task: input.task,
          mode: input.mode,
          candidate_upload_id: input.candidateUploadId,
          llm_session_id: useAppStore.getState().llmSessionId,
        })
        : await startRun(input.task, input.mode, input.candidateUploadId, useAppStore.getState().llmSessionId);
      if (taskVersion !== useAppStore.getState().taskVersion) {
        await cancelRun(accepted.run_id);
        return null;
      }
      return accepted;
    },
    onSuccess: (accepted) => {
      if (!accepted) return;
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
    mutationFn: () => {
      useAppStore.getState().interruptTask();
      return cancelRun(runId);
    },
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
    mutationFn: async (body: RunRevisionRequest) => {
      useAppStore.getState().interruptTask();
      const taskVersion = useAppStore.getState().taskVersion;
      const accepted = await reviseRun(runId, {
        ...body, llm_session_id: useAppStore.getState().llmSessionId,
      });
      if (taskVersion !== useAppStore.getState().taskVersion) {
        await cancelRun(accepted.run_id);
        return null;
      }
      return accepted;
    },
    onSuccess: (accepted) => {
      if (!accepted) return;
      setActiveRunId(accepted.run_id);
      queryClient.invalidateQueries({ queryKey: runKeys.detail(runId) });
      queryClient.invalidateQueries({ queryKey: runKeys.detail(accepted.run_id) });
    },
  });
}

/** Stop the run owned by this browser before another workflow can start. */
export function useStopActiveRun() {
  const queryClient = useQueryClient();
  return async () => {
    const runId = useAppStore.getState().activeRunId;
    if (!runId) return;
    const snapshot = queryClient.getQueryData<RunSnapshot>(runKeys.detail(runId));
    if (snapshot && TERMINAL_RUN_STATUSES.has(snapshot.status)) return;
    const stopped = await cancelRun(runId);
    queryClient.setQueryData(runKeys.detail(runId), stopped);
    if (!TERMINAL_RUN_STATUSES.has(stopped.status)) {
      throw new Error("The previous run is still stopping. Retry before starting another task.");
    }
  };
}
