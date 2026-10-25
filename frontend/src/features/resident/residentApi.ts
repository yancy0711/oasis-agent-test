import type { components } from "../../api/generated";
import type { FeatureCollection } from "geojson";

export type Preferences = components["schemas"]["ResidentPreferences"];
export type Origin = components["schemas"]["ResidentOrigin"];
export type SearchResult = components["schemas"]["ResidentSearchResponse"];
export type Place = components["schemas"]["ResidentPlace"];
export type Category = Place["category"];
export interface ResidentRoute extends FeatureCollection {
  summary: { destination_id: string; destination_name: string; duration_minutes: number;
    distance_km: number; within_threshold: boolean; heat_status: string; message: string;
    green_score?: number | null; green_status?: string; mode?: string };
}

export interface DisplayRoute { route: ResidentRoute; variant: "fastest" | "greener" | "selected"; }

export function recommendedRoutes(routes: ResidentRoute[]) {
  const eligible = routes.filter((route) => route.summary.within_threshold);
  const fastest = [...eligible].sort((a, b) => a.summary.duration_minutes - b.summary.duration_minutes)[0];
  const scored = eligible.filter((route) => route.summary.green_score != null);
  const greenest = [...scored].sort((a, b) => b.summary.green_score! - a.summary.green_score!)[0];
  const greener = fastest?.summary.green_score != null && greenest &&
    greenest.summary.green_score! > fastest.summary.green_score + 0.01 ? greenest : undefined;
  return { fastest, greener };
}

export const CATEGORIES: Record<Category, { label: string; color: string }> = {
  library: { label: "Public libraries", color: "#2563eb" },
  community_centre: { label: "Community centers", color: "#7c3aed" },
  swimming_pool: { label: "Swimming pools", color: "#0891b2" },
  mall: { label: "Shopping malls", color: "#c2410c" },
  sports_centre: { label: "Sports centers", color: "#15803d" },
  arts_centre: { label: "Arts centers", color: "#be185d" },
  place_of_worship: { label: "Places of worship", color: "#854d0e" },
  park: { label: "Parks", color: "#4d7c0f" },
};
export const DEFAULT_PREFERENCES: Preferences = {
  origin_query: null, use_current_location: false, mode: "walking", threshold_minutes: 15,
  categories: Object.keys(CATEGORIES) as Category[],
  excluded_categories: [], preferred_categories: [], indoor_only: false, clarification: "", needs_clarification: false, notice: "",
};

export async function residentPost<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const timeout = AbortSignal.timeout(path.startsWith("cancel/") ? 5_000 : path === "search" ? 125_000 : 60_000);
  const response = await fetch(path.startsWith("/") ? path : `/api/v1/resident/${path}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body), signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
  });
  const payload = await response.json();
  if (!response.ok) {
    const detail = payload && typeof payload === "object" && "detail" in payload ? payload.detail : null;
    throw new Error(typeof detail === "string" ? detail : "The nearby-place request failed. Check the inputs and try again.");
  }
  return payload as T;
}

export type Interpretation = components["schemas"]["RequestInterpretation"];
