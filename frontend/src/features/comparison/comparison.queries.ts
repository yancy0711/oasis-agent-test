import { useQueries } from "@tanstack/react-query";
import { getRun } from "../../api/client";
import { runKeys } from "../../api/queryKeys";

export function useComparisonRuns(runIds: string[]) {
  return useQueries({
    queries: runIds.filter(Boolean).map((runId) => ({
      queryKey: runKeys.detail(runId),
      queryFn: () => getRun(runId),
      staleTime: 30_000,
    })),
  });
}
