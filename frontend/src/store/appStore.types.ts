import type { MobilityMode, PlaceConfirmResponse } from "../api/models";

export type ConfirmedPlace = PlaceConfirmResponse;

export interface AppSessionState {
  activeRunId: string | null;
  confirmedPlace: ConfirmedPlace | null;
  candidateUploadId: string | null;
  llmSessionId: string | null;
  llmSessionLabel: string | null;
  completedRunIds: string[];
  comparisonRunIds: string[];
  mobilitySignature: string | null;
  mobilityRunIds: Partial<Record<MobilityMode, string>>;
}

export interface AppSessionActions {
  setActiveRunId: (runId: string | null) => void;
  setConfirmedPlace: (place: ConfirmedPlace | null) => void;
  setCandidateUploadId: (uploadId: string | null) => void;
  setLlmSession: (sessionId: string | null, label: string | null) => void;
  registerCompletedRun: (runId: string) => void;
  setComparisonRunIds: (runIds: string[]) => void;
  registerMobilityRun: (signature: string, mode: MobilityMode, runId: string) => void;
  resetSession: () => void;
}

export type AppStore = AppSessionState & AppSessionActions;
