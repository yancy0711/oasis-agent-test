import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";
import type { AppSessionState, AppStore } from "./appStore.types";

const initialState: AppSessionState = {
  activeRunId: null,
  confirmedPlace: null,
  candidateUploadId: null,
  llmSessionId: null,
  llmSessionLabel: null,
  completedRunIds: [],
  comparisonRunIds: [],
  mobilitySignature: null,
  mobilityRunIds: {},
};

export const useAppStore = create<AppStore>()(
  persist(
    (set) => ({
      ...initialState,
      setActiveRunId: (activeRunId) => set({ activeRunId }),
      setConfirmedPlace: (confirmedPlace) => set({ confirmedPlace }),
      setCandidateUploadId: (candidateUploadId) => set({ candidateUploadId }),
      setLlmSession: (llmSessionId, llmSessionLabel) => set({ llmSessionId, llmSessionLabel }),
      registerCompletedRun: (runId) => set((state) => ({
        completedRunIds: state.completedRunIds.includes(runId)
          ? state.completedRunIds
          : [...state.completedRunIds, runId],
      })),
      setComparisonRunIds: (comparisonRunIds) => set({
        comparisonRunIds: [...new Set(comparisonRunIds)].slice(-2),
      }),
      registerMobilityRun: (signature, mode, runId) => set((state) => ({
        mobilitySignature: signature,
        mobilityRunIds: {
          ...(state.mobilitySignature === signature ? state.mobilityRunIds : {}),
          [mode]: runId,
        },
      })),
      resetSession: () => set(initialState),
    }),
    {
      name: "oasis-session",
      storage: createJSONStorage(() => sessionStorage),
      partialize: (state) => ({
        activeRunId: state.activeRunId,
        confirmedPlace: state.confirmedPlace,
        candidateUploadId: state.candidateUploadId,
        completedRunIds: state.completedRunIds,
        comparisonRunIds: state.comparisonRunIds,
        mobilitySignature: state.mobilitySignature,
        mobilityRunIds: state.mobilityRunIds,
      }),
    },
  ),
);
