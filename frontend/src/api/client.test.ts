import { afterEach, describe, expect, it, vi } from "vitest";
import { planPlaceRoute, planPointRoute } from "./client";
afterEach(() => vi.unstubAllGlobals());
describe("route requests", () => {
  it("reports a non-JSON gateway error without reading its body twice", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("<html>Gateway timeout</html>", { status: 504 })));
    await expect(planPointRoute("run", -117.37, 33.98, "walking")).rejects.toThrow("HTTP 504");
  });
  it("preserves provider error details", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "ORS unavailable" }), { status: 503 })));
    await expect(planPlaceRoute("run", "92501", "walking")).rejects.toThrow("ORS unavailable");
  });
  it("propagates cancellation", async () => {
    const controller = new AbortController();
    vi.stubGlobal("fetch", vi.fn((_url, init: RequestInit) => new Promise((_resolve, reject) => {
      const signal = init.signal!;
      signal.addEventListener("abort", () => reject(signal.reason), { once: true });
    })));
    const pending = planPlaceRoute("run", "3637 5th St", "walking", controller.signal);
    const assertion = expect(pending).rejects.toMatchObject({ name: "AbortError" });
    controller.abort();
    await assertion;
  });
});
