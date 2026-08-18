export const configKeys = {
  detail: ["config"] as const,
};

export const runKeys = {
  all: ["runs"] as const,
  detail: (runId: string) => ["runs", runId] as const,
  report: (runId: string, name: string) => ["runs", runId, "artifact", name] as const,
};
