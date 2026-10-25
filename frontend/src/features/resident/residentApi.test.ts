import { describe, expect, it } from "vitest";
import { recommendedRoutes, type ResidentRoute } from "./residentApi";
const route = (id: string, minutes: number, green: number | null, within = true): ResidentRoute => ({
  type: "FeatureCollection", features: [], summary: { destination_id: id, destination_name: id,
    duration_minutes: minutes, distance_km: 1, within_threshold: within, heat_status: "unavailable", message: "", green_score: green },
});
describe("route recommendation evidence", () => {
  it("highlights a faster route and a genuinely higher scored eligible route", () => {
    const routes = [route("near", 4, 2), route("green", 6, 8), route("too-far", 20, 10, false)];
    expect(recommendedRoutes(routes).fastest?.summary.destination_id).toBe("near");
    expect(recommendedRoutes(routes).greener?.summary.destination_id).toBe("green");
  });
  it("does not manufacture a green comparison from missing evidence or a tie", () => {
    expect(recommendedRoutes([route("near", 4, null), route("other", 6, 8)]).greener).toBeUndefined();
    expect(recommendedRoutes([route("near", 4, 8), route("other", 6, 8)]).greener).toBeUndefined();
    expect(recommendedRoutes([route("near", 4, 0), route("other", 6, 3)]).greener).toBeDefined();
  });
});
