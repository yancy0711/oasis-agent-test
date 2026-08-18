import { useMutation } from "@tanstack/react-query";
import { confirmPlace, parseTask, resolvePlace, uploadCandidates } from "../../api/client";
import { useAppStore } from "../../store/appStore";

export function useParseTask() {
  return useMutation({ mutationFn: ({ prompt, signal }: { prompt: string; signal: AbortSignal }) =>
    parseTask(prompt, useAppStore.getState().llmSessionId, signal) });
}

export function useResolvePlace() {
  return useMutation({ mutationFn: resolvePlace });
}

export function useConfirmPlace() {
  return useMutation({
    mutationFn: (input: { resolutionId: string; candidateId: string }) =>
      confirmPlace(input.resolutionId, input.candidateId),
  });
}

export function useUploadCandidates() {
  return useMutation({ mutationFn: uploadCandidates });
}
