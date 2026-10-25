import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PropsWithChildren } from "react";
import { cancelRun, getRun, reviseRun, startRun } from "../../api/client";
import type { RunSnapshot, TaskSpec } from "../../api/models";
import { runKeys } from "../../api/queryKeys";
import { useAppStore } from "../../store/appStore";
import { useReviseRun, useStartRun, useStopActiveRun } from "./run.mutations";

vi.mock("../../api/client", () => ({ cancelRun: vi.fn(), getRun: vi.fn(), reviseRun: vi.fn(), startRun: vi.fn() }));
const task = { place_name: "Phoenix" } as TaskSpec;
const input = { task, mode: "quick" as const, candidateUploadId: null };
function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  function wrapper({ children }: PropsWithChildren) { return <QueryClientProvider client={client}>{children}</QueryClientProvider>; }
  return { client, wrapper };
}

beforeEach(() => { vi.clearAllMocks(); useAppStore.getState().resetSession(); });
describe("one active browser task", () => {
  it("retries the server's confirmed task and opens the new run", async () => {
    useAppStore.getState().setActiveRunId("failed-osm");
    vi.mocked(reviseRun).mockResolvedValue({ run_id: "retry-osm", status: "queued", mode: "quick", parent_run_id: "failed-osm" });
    const { wrapper } = setup();
    const { result } = renderHook(() => useReviseRun("failed-osm"), { wrapper });
    await act(async () => { await result.current.mutateAsync({}); });
    expect(reviseRun).toHaveBeenCalledWith("failed-osm", { llm_session_id: null });
    expect(startRun).not.toHaveBeenCalled();
    expect(useAppStore.getState().activeRunId).toBe("retry-osm");
  });
  it("supersedes a previous run even before its first polling result is cached", async () => {
    useAppStore.getState().setActiveRunId("old");
    vi.mocked(getRun).mockResolvedValue({ run_id: "old", status: "running" } as RunSnapshot);
    vi.mocked(reviseRun).mockResolvedValue({ run_id: "new", status: "queued", mode: "quick", parent_run_id: "old" });
    const { wrapper } = setup();
    const { result } = renderHook(useStartRun, { wrapper });
    await act(async () => { await result.current.mutateAsync(input); });
    expect(getRun).toHaveBeenCalledWith("old");
    expect(reviseRun).toHaveBeenCalledWith("old", expect.objectContaining({ task }));
    expect(startRun).not.toHaveBeenCalled();
    expect(useAppStore.getState().activeRunId).toBe("new");
  });

  it("cancels a late run acceptance after the user switched workflows", async () => {
    let accept!: (value: Awaited<ReturnType<typeof startRun>>) => void;
    vi.mocked(startRun).mockReturnValue(new Promise((resolve) => { accept = resolve; }));
    vi.mocked(cancelRun).mockResolvedValue({ run_id: "late", status: "cancelled" } as RunSnapshot);
    const { wrapper } = setup();
    const { result } = renderHook(useStartRun, { wrapper });
    let pending!: Promise<unknown>;
    await act(async () => { pending = result.current.mutateAsync(input); });
    useAppStore.getState().interruptTask();
    await act(async () => { accept({ run_id: "late", status: "queued", mode: "quick" }); await pending; });
    expect(cancelRun).toHaveBeenCalledWith("late");
    expect(useAppStore.getState().activeRunId).toBeNull();
  });

  it("waits for a cancellation acknowledgement and retains the stopped snapshot", async () => {
    useAppStore.getState().setActiveRunId("old");
    let acknowledge!: (value: RunSnapshot) => void;
    vi.mocked(cancelRun).mockReturnValue(new Promise((resolve) => { acknowledge = resolve; }));
    const { wrapper, client } = setup();
    const { result } = renderHook(useStopActiveRun, { wrapper });
    let finished = false;
    const stopping = result.current().then(() => { finished = true; });
    await Promise.resolve();
    expect(finished).toBe(false);
    acknowledge({ run_id: "old", status: "cancelled" } as RunSnapshot);
    await stopping;
    expect(client.getQueryData(runKeys.detail("old"))).toMatchObject({ status: "cancelled" });
  });

  it("keeps a completed result without cancelling it on workflow change", async () => {
    useAppStore.getState().setActiveRunId("saved");
    const { wrapper, client } = setup();
    client.setQueryData(runKeys.detail("saved"), { status: "succeeded" });
    const { result } = renderHook(useStopActiveRun, { wrapper });
    await result.current();
    expect(cancelRun).not.toHaveBeenCalled();
    expect(useAppStore.getState().activeRunId).toBe("saved");
  });
});
