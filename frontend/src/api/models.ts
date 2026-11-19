import type { components } from "./generated";
import type { FeatureCollection } from "geojson";

export type TaskSpec = components["schemas"]["TaskSpec"];
export type RunRequest = components["schemas"]["RunRequest"];
export type RunRevisionRequest = components["schemas"]["RunRevisionRequest"];
export type RunAccepted = components["schemas"]["RunAccepted"];
export type RunSnapshot = components["schemas"]["RunSnapshotResponse"];
export type RunResult = components["schemas"]["RunResultResponse"];
export type RunProgress = components["schemas"]["RunProgress"];
export type PublicAgentEvent = components["schemas"]["PublicAgentEvent"];
export type ParseResponse = components["schemas"]["ParseResponse"];
export type RouteOriginCandidate = components["schemas"]["RouteOriginCandidate"];
export type RequestDisposition = ParseResponse["disposition"];
export type PlaceCandidate = components["schemas"]["PlaceCandidate"];
export type PlaceResolveResponse = components["schemas"]["PlaceResolveResponse"];
export type PlaceConfirmResponse = Omit<components["schemas"]["PlaceConfirmResponse"], "boundary"> & { boundary?: FeatureCollection | null };
export type UploadResponse = components["schemas"]["UploadResponse"];
export type UiConfigResponse = components["schemas"]["UiConfigResponse"];
export type DataSourceStatus = components["schemas"]["DataSourceStatusResponse"];
export type OfficialResources = components["schemas"]["OfficialResourcesResponse"];
export type RoutingMode = "quick" | "precise" | "auto";
export type MobilityMode = TaskSpec["mobility_scenario"];

interface RegionalDataInfo {
  place_name: string;
  units: number;
  year: number | null;
  geometry: FeatureCollection;
}

export interface RegionalCensus extends RegionalDataInfo {
  source: string;
  source_url: string;
  older_units: number;
  notes?: string;
}

export interface DemandUpload extends RegionalDataInfo {
  upload_id: string;
  filename: string;
  fields: { older?: string };
  boundary?: FeatureCollection;
}

export type StudyInputLayers = Partial<Record<"boundary" | "tract" | "candidate", FeatureCollection>>;
