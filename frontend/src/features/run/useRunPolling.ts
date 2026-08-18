import { useQuery } from "@tanstack/react-query";
import { getRun } from "../../api/client";
import { runKeys } from "../../api/queryKeys";
import { TERMINAL_RUN_STATUSES } from "./run.constants";

export function useRunPolling(runId: string | null) {
  return useQuery({
    queryKey: runId ? runKeys.detail(runId) : ["runs", "inactive"],
    queryFn: () => getRun(runId!),
    enabled: Boolean(runId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status && TERMINAL_RUN_STATUSES.has(status) ? false : 1_000;
    },
    refetchIntervalInBackground: false,
  });
}
