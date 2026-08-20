import createClient from "openapi-fetch";
import type { Feature } from "geojson";
import type { paths } from "./generated";
import { apiError } from "./errors";
import type {
  ParseResponse,
  DataSourceStatus,
  OfficialResources,
  PlaceConfirmResponse,
  PlaceResolveResponse,
  RouteOriginCandidate,
  RoutingMode,
  RunAccepted,
  RunRevisionRequest,
  RunSnapshot,
  TaskSpec,
  UiConfigResponse,
  UploadResponse,
} from "./models";

const client = createClient<paths>({ baseUrl: "/api" });

function valueOrThrow<T>(data: T | undefined, error: unknown, fallback: string): T {
  if (error || data === undefined) throw apiError(error, fallback);
  return data;
}

export async function getConfig(): Promise<UiConfigResponse> {
  const { data, error } = await client.GET("/v1/config");
  return valueOrThrow(data, error, "Could not load application configuration.");
}

export async function getDataSourceStatus(): Promise<DataSourceStatus> {
  const { data, error } = await client.GET("/v1/data-sources/status");
  return valueOrThrow(data, error, "Could not check approved data sources.");
}

export async function getOfficialResources(placeName: string): Promise<OfficialResources> {
  const { data, error } = await client.GET("/v1/official-resources", { params: { query: { place_name: placeName } } });
  return valueOrThrow(data, error, "Could not check official cooling resources.");
}

export interface StreetViewResult {
  available: boolean;
  status: "available" | "no_image" | "not_configured" | "error";
  provider: string;
  message: string;
  image_id?: string | null;
  image_url?: string | null;
  viewer_url?: string | null;
  captured_at?: string | number | null;
  creator?: string | null;
  latitude?: number | null;
  longitude?: number | null;
  distance_m?: number | null;
}

export async function getStreetView(latitude: number, longitude: number, radiusM = 100): Promise<StreetViewResult> {
  const query = new URLSearchParams({
    lat: String(latitude), lon: String(longitude), radius_m: String(radiusM),
  });
  const response = await fetch(`/api/streetview?${query}`, { signal: AbortSignal.timeout(40_000) });
  if (!response.ok) throw new Error(`Street-level imagery request failed (${response.status}).`);
  return response.json() as Promise<StreetViewResult>;
}

export interface LlmConnectionInput {
  provider: "openai" | "openrouter" | "custom" | "local";
  base_url: string;
  model: string;
  api_key: string;
}
export interface LlmConnectionResult {
  session_id: string; provider: string; model: string; latency_ms: number;
  resolved_model: string; expires_in_seconds: number; structured_output: boolean;
}

export async function testLlmConnection(input: LlmConnectionInput): Promise<LlmConnectionResult> {
  const response = await fetch("/api/v1/llm/test", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input),
  });
  if (!response.ok) {
    let detail: unknown;
    try { detail = await response.json(); } catch { detail = await response.text(); }
    throw apiError(detail, "Could not connect to this model.");
  }
  return response.json() as Promise<LlmConnectionResult>;
}

export async function listLlmModels(input: Omit<LlmConnectionInput, "model">): Promise<string[]> {
  const response = await fetch("/api/v1/llm/models", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input),
  });
  if (!response.ok) {
    let detail: unknown;
    try { detail = await response.json(); } catch { detail = await response.text(); }
    throw apiError(detail, "Could not read available models.");
  }
  const result = await response.json() as { models: string[] };
  return result.models;
}

export async function revokeLlmSession(sessionId: string): Promise<void> {
  await fetch(`/api/v1/llm/sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE", keepalive: true,
  });
}

export async function parseTask(prompt: string, llmSessionId?: string | null, signal?: AbortSignal): Promise<ParseResponse> {
  const response = await fetch("/api/v1/parse", {
    method: "POST", signal, headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt, llm_session_id: llmSessionId ?? null }),
  });
  if (!response.ok) {
    let detail: unknown;
    try { detail = await response.json(); } catch { detail = await response.text(); }
    throw apiError(detail, "Could not parse the request.");
  }
  return response.json() as Promise<ParseResponse>;
}

export async function resolvePlace(placeName: string): Promise<PlaceResolveResponse> {
  const { data, error } = await client.POST("/v1/places/resolve", {
    body: { place_name: placeName },
  });
  return valueOrThrow(data, error, "Could not resolve the study area.");
}

export async function confirmPlace(
  resolutionId: string,
  candidateId: string,
): Promise<PlaceConfirmResponse> {
  const { data, error } = await client.POST("/v1/places/confirm", {
    body: { resolution_id: resolutionId, candidate_id: candidateId },
  });
  return valueOrThrow(data, error, "Could not confirm the study area.");
}

export interface CoordinateOriginResult {
  outside_us: boolean;
  message: string;
  candidates: RouteOriginCandidate[];
}

export async function reverseRouteOrigin(
  longitude: number,
  latitude: number,
): Promise<CoordinateOriginResult> {
  const response = await fetch("/api/v1/places/reverse-origin", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ longitude, latitude }),
  });
  if (!response.ok) {
    let detail: unknown;
    try { detail = await response.json(); } catch { detail = await response.text(); }
    throw apiError(detail, "Could not identify the current location.");
  }
  return response.json() as Promise<CoordinateOriginResult>;
}

export async function uploadCandidates(file: File): Promise<UploadResponse> {
  const form = new FormData();
  form.set("file", file);
  const response = await fetch("/api/v1/uploads", { method: "POST", body: form });
  if (!response.ok) {
    let detail: unknown;
    try { detail = await response.json(); } catch { detail = await response.text(); }
    throw apiError(detail, "Could not upload candidate facilities.");
  }
  return response.json() as Promise<UploadResponse>;
}

export async function startRun(
  task: TaskSpec,
  mode: RoutingMode,
  candidateUploadId: string | null,
  llmSessionId?: string | null,
): Promise<RunAccepted> {
  const { data, error } = await client.POST("/v1/runs", {
    body: { task, mode, candidate_upload_id: candidateUploadId, llm_session_id: llmSessionId ?? null },
  });
  return valueOrThrow(data, error, "Could not start the run.");
}

export async function getRun(runId: string): Promise<RunSnapshot> {
  const { data, error } = await client.GET("/v1/runs/{run_id}", {
    params: { path: { run_id: runId } },
  });
  return valueOrThrow(data, error, "Could not load the run.");
}

export async function cancelRun(runId: string): Promise<RunSnapshot> {
  const { data, error } = await client.POST("/v1/runs/{run_id}/cancel", {
    params: { path: { run_id: runId } },
  });
  return valueOrThrow(data, error, "Could not cancel the run.");
}

export async function reviseRun(
  runId: string,
  body: RunRevisionRequest,
): Promise<RunAccepted> {
  const { data, error } = await client.POST("/v1/runs/{run_id}/revisions", {
    params: { path: { run_id: runId } },
    body,
  });
  return valueOrThrow(data, error, "Could not revise the run.");
}

export function artifactUrl(runId: string, name: string): string {
  return `/api/v1/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(name)}`;
}

export interface PlannedRoute {
  type: "FeatureCollection";
  features: Feature[];
  summary: Record<string, unknown>;
}

export async function planPointRoute(
  runId: string, longitude: number, latitude: number, mode: "walking" | "cycling" | "driving",
): Promise<PlannedRoute> {
  const response = await fetch(`/api/v1/runs/${encodeURIComponent(runId)}/routes`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ longitude, latitude, mode }),
  });
  if (!response.ok) {
    let detail: unknown;
    try { detail = await response.json(); } catch { detail = await response.text(); }
    throw apiError(detail, "Could not plan a route from this point.");
  }
  return response.json() as Promise<PlannedRoute>;
}

export async function planPlaceRoute(
  runId: string, query: string, mode: "walking" | "cycling" | "driving",
): Promise<PlannedRoute> {
  const response = await fetch(
    `/api/v1/runs/${encodeURIComponent(runId)}/routes/from-place`,
    {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, mode }),
    },
  );
  if (!response.ok) {
    let detail: unknown;
    try { detail = await response.json(); } catch { detail = await response.text(); }
    throw apiError(detail, "Could not plan a route from this location.");
  }
  return response.json() as Promise<PlannedRoute>;
}

export function publicApiUrl(path: string | undefined): string | undefined {
  if (!path) return undefined;
  if (path.startsWith("/api/")) return path;
  if (path.startsWith("/v1/")) return `/api${path}`;
  return path;
}
