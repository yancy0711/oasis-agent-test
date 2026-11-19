import type { MobilityMode, PlaceConfirmResponse } from "../api/models";

export type ConfirmedPlace = PlaceConfirmResponse;
export type UploadedPoint = [longitude: number, latitude: number, name: string];
export type StudyLayer = "boundary" | "demand" | "heat" | "candidate" | "uploaded";

export interface AppSessionState {
  audience: "research" | "personal";
  activeRunId: string | null;
  taskVersion: number;
  confirmedPlace: ConfirmedPlace | null;
  candidateUploadId: string | null;
  candidateUploadName: string | null;
  uploadedPoints: UploadedPoint[];
  studyLayerVisibility: Record<StudyLayer, boolean>;
  llmSessionId: string | null;
  llmSessionLabel: string | null;
  completedRunIds: string[];
  comparisonRunIds: string[];
  mobilitySignature: string | null;
  mobilityRunIds: Partial<Record<MobilityMode, string>>;
}

export interface AppSessionActions {
  interruptTask: () => void;
  setAudience: (audience: "research" | "personal") => void;
  setActiveRunId: (runId: string | null) => void;
  setConfirmedPlace: (place: ConfirmedPlace | null) => void;
  setCandidateUploadId: (uploadId: string | null, points?: UploadedPoint[], filename?: string) => void;
  setStudyLayerVisibility: (layer: StudyLayer, visible: boolean) => void;
  setLlmSession: (sessionId: string | null, label: string | null) => void;
  registerCompletedRun: (runId: string) => void;
  setComparisonRunIds: (runIds: string[]) => void;
  registerMobilityRun: (signature: string, mode: MobilityMode, runId: string) => void;
  resetSession: () => void;
}

export type AppStore = AppSessionState & AppSessionActions;
