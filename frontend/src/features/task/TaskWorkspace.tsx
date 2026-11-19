import { useWorkspacePreferences } from "../../app/WorkspacePreferences";
import { useCallback, useRef, useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import type { DemandUpload, PlaceResolveResponse, RouteOriginCandidate, RoutingMode, TaskSpec } from "../../api/models";
import type { RequestDisposition } from "../../api/models";
import { getConfig, getRegionalCensus, getStudyBoundary, getStudyInputLayers, reverseRouteOrigin, searchRouteOrigin } from "../../api/client";
import { configKeys } from "../../api/queryKeys";
import { Workbench, type WorkspaceTab } from "../../app/Workbench";
import { WorkspaceIcon } from "../../app/WorkspaceIcon";
import { useAppStore } from "../../store/appStore";
import { useStartRun, useStopActiveRun } from "../run/run.mutations";
import { isTerminalRun, visibleResult } from "../run/run.selectors";
import { useRunPolling } from "../run/useRunPolling";
import { PlaceConfirmation } from "./PlaceConfirmation";
import { PromptInput } from "./PromptInput";
import { LlmSettings } from "./LlmSettings";
import { CandidateUpload } from "./CandidateUpload";
import { RegionalDataUpload, type DemandUploadHandle } from "./RegionalData";
import { TaskForm, type ValueProfile } from "./TaskForm";
import { taskFromDraft, toTaskSpec } from "./taskForm.mapper";
import type { TaskFormValues } from "./taskForm.schema";
import {
  useConfirmPlace,
  useParseTask,
  useResolvePlace,
  useUploadCandidates,
} from "./task.mutations";

import { ResidentWorkspace, type ResidentRequest } from "../resident/ResidentWorkspace";
import { residentPost, type Interpretation, type Preferences } from "../resident/residentApi";

const regionalDataHelp = "Check the regional population layer in Data or upload demand polygons.";
const placeKey = (place: string) => place.normalize("NFKC").toLowerCase().replace(/[\s,，]+/g, "");

export function TaskWorkspace() {
  const { t } = useWorkspacePreferences();
  const audience = useAppStore((state) => state.audience);
  const [tab, setTab] = useState<WorkspaceTab>("Data");
  const showResults = useCallback(() => setTab("Results"), []);
  const setAudience = useAppStore((state) => state.setAudience);
  const [residentRequest, setResidentRequest] = useState<ResidentRequest | null>(null);
  const [residentBusy, setResidentBusy] = useState(false);
  const [residentCancel, setResidentCancel] = useState(0);
  const [interpreting, setInterpreting] = useState(false);
  const [intentMessage, setIntentMessage] = useState("");
  const [intentError, setIntentError] = useState("");
  const [autoDetect, setAutoDetect] = useState(true);
  const entryController = useRef<AbortController | null>(null);
  const residentPreferences = useRef<Preferences | null>(null);
  const residentStop = useRef<(() => Promise<void>) | null>(null);
  const rememberResidentStop = useCallback((stop: (() => Promise<void>) | null) => { residentStop.current = stop; }, []);
  const rememberPreferences = useCallback((value: Preferences) => { residentPreferences.current = value; }, []);
  const [task, setTask] = useState<TaskSpec | null>(null);
  const [resolution, setResolution] = useState<PlaceResolveResponse | null>(null);
  const [mode, setMode] = useState<RoutingMode>("auto");
  const [manualPlace, setManualPlace] = useState("");
  const [originRefinement, setOriginRefinement] = useState("");
  const [selectedRouteOriginId, setSelectedRouteOriginId] = useState<string | null>(null);
  const [routeOriginCandidates, setRouteOriginCandidates] = useState<RouteOriginCandidate[]>([]);
  const [placeNameOptions, setPlaceNameOptions] = useState<string[]>([]);
  const [demandUpload, setDemandUpload] = useState<DemandUpload | null>(null);
  const [candidateCrs, setCandidateCrs] = useState("4326");
  const [candidateEpsg, setCandidateEpsg] = useState("");
  const [candidateCrsError, setCandidateCrsError] = useState("");
  const candidateSourceCrs = candidateCrs === "custom" ? candidateEpsg : candidateCrs;
  const demandControl = useRef<DemandUploadHandle>(null);
  const [decision, setDecision] = useState<{ disposition: RequestDisposition; reason: string; alternative: string } | null>(null);
  const [draftLocked, setDraftLocked] = useState(false);
  const [candidateChangedForRun, setCandidateChangedForRun] = useState<string | null>(null);
  const [routeStepOpen, setRouteStepOpen] = useState(true);
  const parseController = useRef<AbortController | null>(null);
  const confirmedPlace = useAppStore((state) => state.confirmedPlace);
  const candidateUploadId = useAppStore((state) => state.candidateUploadId);
  const storedUploadName = useAppStore((state) => state.candidateUploadName);
  const uploadedName = candidateUploadId ? storedUploadName ?? t("Candidate CSV") : null;
  const uploadedPointCount = useAppStore((state) => state.uploadedPoints.length);
  const activeRunId = useAppStore((state) => state.activeRunId);
  const setConfirmedPlace = useAppStore((state) => state.setConfirmedPlace);
  const setCandidateUploadId = useAppStore((state) => state.setCandidateUploadId);

  const parseMutation = useParseTask();
  const resolveMutation = useResolvePlace();
  const confirmMutation = useConfirmPlace();
  const uploadMutation = useUploadCandidates();
  const startMutation = useStartRun();
  const stopActiveRun = useStopActiveRun();
  const activeRunQuery = useRunPolling(activeRunId);
  const candidateSelectionLocked = draftLocked && Boolean(
    activeRunId && (!activeRunQuery.data || !isTerminalRun(activeRunQuery.data))
  );
  const configQuery = useQuery({ queryKey: configKeys.detail, queryFn: getConfig, staleTime: Infinity });
  const selectedDemand = demandUpload?.upload_id === task?.demand_upload_id ? demandUpload : null;
  const regionalPlace = confirmedPlace && /^(hk|sz):/.test(confirmedPlace.candidate_id) ? confirmedPlace.canonical_name : null;
  const regionalQuery = useQuery({
    queryKey: ["regional-census", regionalPlace],
    queryFn: ({ signal }) => getRegionalCensus(regionalPlace!, signal),
    enabled: Boolean(regionalPlace && !selectedDemand),
    retry: false,
    staleTime: Infinity,
  });
  const regionalData = selectedDemand ?? regionalQuery.data;
  const boundaryQuery = useQuery({
    queryKey: ["regional-boundary", regionalPlace],
    queryFn: ({ signal }) => getStudyBoundary(regionalPlace!, signal),
    enabled: Boolean(regionalPlace && !selectedDemand),
    retry: false,
    staleTime: Infinity,
  });
  const snapshot = activeRunQuery.data;
  const resultMap = visibleResult(snapshot)?.artifacts?.map;
  const matchingArea = Boolean(snapshot && (!task ||
    (snapshot.task.place_name === task.place_name && (snapshot.task.demand_upload_id ?? null) === (task.demand_upload_id ?? null))));
  const matchingRun = matchingArea && snapshot?.run_id !== candidateChangedForRun;
  const finished = isTerminalRun(snapshot);
  const liveInputs = useQuery({
    queryKey: ["study-inputs", activeRunId, finished],
    queryFn: ({ signal }) => getStudyInputLayers(activeRunId!, signal),
    enabled: Boolean(activeRunId && matchingArea && !resultMap),
    refetchInterval: finished ? false : 2400,
    placeholderData: (previous, query) => query?.queryKey[1] === activeRunId ? previous : undefined,
    retry: false,
  });

  const profiles: ValueProfile[] = Object.entries(configQuery.data?.value_profiles ?? {}).flatMap(([name, raw]) => {
    if (!raw || typeof raw !== "object") return [];
    const record = raw as Record<string, unknown>;
    const weights = record.weights;
    if (!weights || typeof weights !== "object") return [];
    const values = weights as Record<string, unknown>;
    if (!["population_density", "elderly_ratio", "poverty_rate", "heat_exposure", "environment_burden"].every((key) => typeof values[key] === "number")) return [];
    return [{ name, label: typeof record.label === "string" ? record.label : name.replaceAll("_", " "), weights: values as ValueProfile["weights"] }];
  });

  const parseError = parseMutation.error instanceof DOMException && parseMutation.error.name === "AbortError"
    ? null : parseMutation.error;
  const error = parseError ?? confirmMutation.error
    ?? uploadMutation.error ?? startMutation.error;

  async function resolve(name: string, signal?: AbortSignal) {
    try {
      const value = await resolveMutation.mutateAsync(name);
      if (signal?.aborted) return null;
      setResolution(value);
      return value;
    } catch {
      if (signal?.aborted) return null;
      setResolution(null);
      setDecision({
        disposition: "needs_clarification",
        reason: `An automatic study boundary is unavailable for ${name}.`,
        alternative: t(regionalDataHelp),
      });
      return null;
    }
  }

  async function analyze(prompt: string) {
    parseController.current?.abort();
    const controller = new AbortController();
    parseController.current = controller;
    setConfirmedPlace(null);
    setResolution(null);
    setRouteOriginCandidates([]);
    setPlaceNameOptions([]);
    setSelectedRouteOriginId(null);
    setDraftLocked(false);
    try {
      const parsed = await parseMutation.mutateAsync({ prompt, signal: controller.signal });
      if (controller.signal.aborted) return;
      const parsedDecision = {
        disposition: parsed.disposition,
        reason: parsed.reason,
        alternative: parsed.alternative,
      };
      setRouteOriginCandidates(parsed.route_origin_candidates ?? []);
      setPlaceNameOptions(parsed.place_name_options ?? []);
      if (!parsed.task) {
        setDecision(parsedDecision);
        setTask(null);
        setTab((current) => current === "Setup" ? "Data" : current);
        return;
      }
      const nextTask = taskFromDraft(parsed.task);
      setTask(nextTask);
      setTab("Setup");
      const needsLocationChoice = Boolean(parsed.route_origin_candidates?.length || parsed.place_name_options?.length);
      if (demandUpload && !needsLocationChoice &&
          (placeKey(nextTask.place_name) === placeKey(demandUpload.place_name) ||
           (!nextTask.place_name && selectedDemand))) {
        confirmDemand(demandUpload, false);
        setDecision({ ...parsedDecision, disposition: "full_analysis",
          reason: t("The confirmed upload supplies the study boundary and population."), alternative: "" });
      } else if (nextTask.place_name && !needsLocationChoice) {
        setDecision({
          disposition: "needs_clarification",
          reason: t("Checking the study boundary and available data sources."),
          alternative: "",
        });
        const resolved = await resolve(nextTask.place_name, controller.signal);
        if (controller.signal.aborted) return;
        if (resolved?.selected && !resolved.ambiguous && demandUpload &&
            placeKey(resolved.selected.canonical_name) === placeKey(demandUpload.place_name)) {
          confirmDemand(demandUpload, false);
        }
        if (resolved) setDecision(parsedDecision);
      } else setDecision(parsedDecision);
    } catch (reason) {
      if (!(reason instanceof DOMException && reason.name === "AbortError")) throw reason;
    } finally {
      if (parseController.current === controller) parseController.current = null;
    }
  }

  async function submitPrompt(prompt: string) {
    const stopping = interruptWork();
    const controller = new AbortController();
    entryController.current = controller;
    setInterpreting(true); setIntentError(""); setIntentMessage("Identifying the requested workflow...");
    try {
      await stopping;
      controller.signal.throwIfAborted();
      if (!autoDetect) {
        setIntentMessage(`${audience === "research" ? "Research and planning" : "Nearby-place search"}: manually selected.`);
        if (audience === "research") { setResidentRequest(null); await analyze(prompt); }
        else setResidentRequest({ id: crypto.randomUUID(), prompt });
        return;
      }
      const interpretation = await residentPost<Interpretation>("/api/v1/requests/interpret", {
        prompt, audience, previous: audience === "personal" ? residentPreferences.current : null,
        llm_session_id: useAppStore.getState().llmSessionId,
      }, controller.signal);
      controller.signal.throwIfAborted();
      if (interpretation.workflow === "clarify") { setIntentMessage(interpretation.reason); return; }
      const next = interpretation.workflow;
      setIntentMessage(`${next === "personal" ? "Nearby-place search" : "Research and planning"}${next !== audience ? " - switched workspace" : ""}: ${interpretation.reason}`);
      setAudience(next);
      if (next === "personal" && interpretation.preferences) {
        setResidentRequest({ id: crypto.randomUUID(), prompt, preferences: interpretation.preferences });
      } else {
        setResidentRequest(null);
        await analyze(prompt);
      }
    } catch (reason) {
      if (!controller.signal.aborted) setIntentError(reason instanceof Error ? reason.message : "Could not identify the task. Please retry.");
    } finally {
      if (entryController.current === controller) { entryController.current = null; setInterpreting(false); }
    }
  }

  function cancelAnalysis() {
    entryController.current?.abort(); entryController.current = null; setInterpreting(false);
    parseController.current?.abort();
    parseController.current = null;
    parseMutation.reset();
  }

  function interruptWork() {
    cancelAnalysis();
    useAppStore.getState().interruptTask();
    const nearbyStopped = residentStop.current?.();
    setResidentCancel((n) => n + 1);
    return Promise.all([stopActiveRun(), nearbyStopped]);
  }

  async function changeAudience(next: "research" | "personal") {
    if (next === audience) {
      cancelAnalysis(); setAutoDetect(false); setIntentMessage(""); setIntentError("");
      return;
    }
    const stopping = interruptWork();
    const controller = new AbortController();
    entryController.current = controller;
    setInterpreting(true); setIntentMessage(""); setIntentError("");
    try {
      await stopping;
      controller.signal.throwIfAborted();
      setResidentRequest(null);
      setAudience(next);
      setAutoDetect(false);
    } catch (reason) {
      if (!controller.signal.aborted) setIntentError(reason instanceof Error ? reason.message : "Could not stop the previous task.");
    } finally {
      if (entryController.current === controller) { entryController.current = null; setInterpreting(false); }
    }
  }

  async function handleUseLocation() {
    const stopping = interruptWork();
    const controller = new AbortController();
    entryController.current = controller;
    setInterpreting(true); setIntentError(""); setIntentMessage("");
    try {
      await stopping;
      controller.signal.throwIfAborted();
      if (audience === "personal") setResidentRequest({ id: crypto.randomUUID(), prompt: "", locate: true });
      else handleCurrentLocation();
    } catch (reason) {
      if (!controller.signal.aborted) setIntentError(reason instanceof Error ? reason.message : "Could not stop the previous task.");
    } finally {
      if (entryController.current === controller) { entryController.current = null; setInterpreting(false); }
    }
  }

  async function confirmResolution(target: PlaceResolveResponse, candidateId: string) {
    const place = await confirmMutation.mutateAsync({
      resolutionId: target.resolution_id,
      candidateId,
    });
    if (demandUpload && placeKey(place.canonical_name) === placeKey(demandUpload.place_name)) {
      confirmDemand(demandUpload, false);
      return;
    }
    setConfirmedPlace(place);
    setTask((current) => current ? {
      ...current, place_name: place.canonical_name,
      demand_upload_id: current.place_name === place.canonical_name ? current.demand_upload_id : null,
    } : current);
  }

  function confirmDemand(data: DemandUpload, showData = true) {
    setDemandUpload(data);
    setResolution(null);
    setPlaceNameOptions([]);
    setConfirmedPlace({
      candidate_id: `demand:${data.upload_id}`, canonical_name: data.place_name,
      display_name: data.place_name, place_type: "uploaded", label: data.place_name, query: data.place_name,
    });
    setTask((current) => {
      const draft = current ?? taskFromDraft({});
      return { ...draft, place_name: data.place_name, place_names: [data.place_name], demand_upload_id: data.upload_id,
        ambiguities: draft.ambiguities?.filter((item) => item.field !== "place_names") };
    });
    resolveMutation.reset();
    if (showData) {
      setIntentError("");
      setDecision({ disposition: "full_analysis", reason: t("The confirmed upload supplies the study boundary and population."), alternative: "" });
      setTab("Data");
    }
  }

  async function confirm(candidateId: string) {
    if (resolution) await confirmResolution(resolution, candidateId);
  }

  async function chooseStudyArea(placeName: string) {
    const remainingAmbiguities = task?.ambiguities?.filter((item) => item.field !== "place_names") ?? [];
    setConfirmedPlace(null);
    setResolution(null);
    setTask((current) => current ? {
      ...current, place_name: placeName, place_names: [placeName], ambiguities: remainingAmbiguities, demand_upload_id: null,
    } : current);
    setPlaceNameOptions([]);
    setDecision({
      disposition: "needs_clarification",
      reason: remainingAmbiguities.length
        ? remainingAmbiguities.map((item) => item.question).join(" ")
        : `Resolving ${placeName} as the single study area for this run.`,
      alternative: remainingAmbiguities.length
        ? "Review the corresponding editable task fields before running."
        : "Run the other named area separately if a cross-city comparison is required.",
    });
    await resolve(placeName);
  }

  function submitManualPlace(event: FormEvent) {
    event.preventDefault();
    const value = manualPlace.trim();
    if (value) void resolve(value);
  }

  async function chooseRouteOrigin(candidate: RouteOriginCandidate) {
    setSelectedRouteOriginId(candidate.candidate_id);
    setResolution(null);
    setConfirmedPlace(null);
    setTask((current) => current ? {
      ...current,
      place_name: candidate.place_name,
      route_origin: candidate.administrative ? null : {
        entity: candidate.display_name,
        context: candidate.place_name,
        positional_hint: candidate.positional_hint,
        raw_text: current.route_origin?.raw_text ?? candidate.display_name,
        source: current.route_origin?.source ?? "llm",
        longitude: candidate.longitude ?? current.route_origin?.longitude ?? null,
        latitude: candidate.latitude ?? current.route_origin?.latitude ?? null,
      },
    } : current);
    const resolved = await resolve(candidate.place_name);
    if (!resolved) return;
    if (resolved.selected) {
      await confirmResolution(resolved, resolved.selected.candidate_id);
      setResolution(null);
    }
  }

  function handleCurrentLocation() {
    setConfirmedPlace(null);
    setResolution(null);
    setRouteOriginCandidates([]);
    setSelectedRouteOriginId(null);
    setDraftLocked(false);
    if (!navigator.geolocation) {
      setTask(null);
      setDecision({
        disposition: "out_of_scope",
        reason: "This browser does not provide geolocation.",
        alternative: "Use a browser with location access or choose the Landmark route example.",
      });
      return;
    }
    setDecision({
      disposition: "needs_clarification",
      reason: "Waiting for browser location permission.",
      alternative: "",
    });
    navigator.geolocation.getCurrentPosition(
      ({ coords }) => {
        const coordinate = coords.latitude.toFixed(5) + ", " + coords.longitude.toFixed(5);
        setDecision({
          disposition: "needs_clarification",
          reason: "Location received (" + coordinate + "). Identifying the smallest supported boundary.",
          alternative: "",
        });
        void reverseRouteOrigin(coords.longitude, coords.latitude).then((result) => {
          if (result.outside_us || !result.candidates.length) {
            setTask(null);
            setRouteOriginCandidates([]);
            setDecision({
              disposition: result.outside_us ? "out_of_scope" : "needs_clarification",
              reason: result.message,
              alternative: result.outside_us
                ? "Choose a US landmark instead; no address entry is required for this result."
                : "Choose a named US landmark or city.",
            });
            return;
          }
          const first = result.candidates[0];
          setTask(taskFromDraft({
            place_name: "",
            route_origin: {
              entity: first.entity,
              context: first.context,
              positional_hint: "",
              raw_text: "Use my current location to find the nearest cooling facility.",
              source: "fallback",
              longitude: first.longitude,
              latitude: first.latitude,
            },
            notes: "Browser location supplied by the user.",
          }));
          setRouteOriginCandidates(result.candidates);
          setTab("Setup");
          setDecision({
            disposition: "needs_clarification",
            reason: result.message,
            alternative: "The smallest supported administrative unit is listed first.",
          });
        }).catch((reason: unknown) => {
          setTask(null);
          setDecision({
            disposition: "out_of_scope",
            reason: reason instanceof Error ? reason.message : "Could not identify this location.",
            alternative: "Choose a named US landmark instead.",
          });
        });
      },
      (reason) => {
        setTask(null);
        setDecision({
          disposition: "out_of_scope",
          reason: reason.code === 1
            ? "Location permission was denied."
            : "The browser could not determine the current location.",
          alternative: "Allow location access or choose the Landmark route example.",
        });
      },
      { enableHighAccuracy: true, timeout: 10000 },
    );
  }

  async function refineRouteOrigin(event: FormEvent) {
    event.preventDefault();
    const value = originRefinement.trim();
    if (!value || candidateSelectionLocked) return;
    try {
      const result = await searchRouteOrigin(value);
      setSelectedRouteOriginId(null);
      setRouteOriginCandidates(result.candidates);
      setDecision({
        disposition: "needs_clarification",
        reason: result.message,
        alternative: result.candidates.length
          ? "Choose one origin below; the existing route-planning task is unchanged."
          : "Enter a more specific US address or landmark.",
      });
    } catch (reason) {
      setDecision({
        disposition: "needs_clarification",
        reason: reason instanceof Error ? reason.message : "Could not search for this route origin.",
        alternative: "Enter a more specific US address or landmark.",
      });
    }
  }

  async function upload(file: File, sourceCrs = "4326") {
    if (sourceCrs !== "gcj02" && !/^[1-9]\d*$/.test(sourceCrs)) {
      setCandidateCrsError(t("Enter a valid EPSG code for candidate coordinates."));
      return;
    }
    setCandidateCrsError("");
    try {
      const result = await uploadMutation.mutateAsync({ file, sourceCrs });
      setCandidateUploadId(result.upload_id, result.preview_points, result.filename);
      if (result.preview_points?.length && snapshot?.task.candidate_source === "csv") setCandidateChangedForRun(snapshot.run_id);
    } catch {
      // The mutation exposes the upload failure in the assistant and the Data tab.
    }
  }

  function removeCandidateUpload() {
    setCandidateUploadId(null);
    setCandidateChangedForRun(null);
    setTask((current) => current?.candidate_source === "csv" ? { ...current, candidate_source: "osm" } : current);
    uploadMutation.reset();
    setCandidateCrsError("");
  }

  function removeDemandUpload() {
    if (!selectedDemand) return;
    setDemandUpload(null);
    setConfirmedPlace(null);
    setTask((current) => current ? { ...current, demand_upload_id: null } : current);
    setDecision(null);
    void resolve(selectedDemand.place_name).catch(() => undefined);
  }

  function run(values: TaskFormValues) {
    if (!task || !confirmedPlace || interpreting) return;
    if (regionalPlace && !selectedDemand && !regionalQuery.data) {
      setIntentError(t(regionalDataHelp));
      setTab("Data");
      return;
    }
    if (configQuery.data?.utci_configured === false) {
      setIntentError(t("UTCI needs a data source. See Data for setup."));
      setTab("Data");
      return;
    }
    setIntentError("");
    useAppStore.getState().interruptTask();
    setDraftLocked(true);
    const executable = toTaskSpec(task, values, confirmedPlace.canonical_name);
    startMutation.mutate({
      task: executable,
      mode,
      candidateUploadId: values.candidate_source === "csv" ? candidateUploadId : null,
    });
  }

  function resolveAmbiguity(index: number, candidate: string) {
    setTask((current) => {
      if (!current) return current;
      const ambiguity = current.ambiguities?.[index];
      if (!ambiguity) return current;
      const next: TaskSpec = { ...current };
      let applied = false;
      const normalizedCandidate = candidate.trim().toLowerCase().replaceAll(" ", "_");
      const goalAliases: Partial<Record<string, TaskSpec["analysis_goal"]>> = {
        fixed_facility_budget: "facility_siting",
        fixed_budget: "facility_siting",
        calculate_minimum_number: "minimum_facilities",
        minimum_facility_count: "minimum_facilities",
      };
      if (ambiguity.field === "n_facilities") {
        const value = Number(candidate);
        if (Number.isInteger(value) && value >= 1 && value <= 100) {
          next.n_facilities = value;
          applied = true;
        }
      } else if (ambiguity.field === "threshold_minutes") {
        const value = Number(candidate);
        if (Number.isFinite(value) && value > 0) {
          next.threshold_minutes = value;
          applied = true;
        }
      }
      else if (ambiguity.field === "mobility_scenario" && ["walking", "cycling", "driving", "walk_drive_equity"].includes(candidate)) {
        next.mobility_scenario = candidate as TaskSpec["mobility_scenario"];
        applied = true;
      } else if (ambiguity.field === "analysis_goal" && [
        "facility_siting", "tract_review", "heat_review", "minimum_facilities",
        "minimum_threshold", "reachability_review", "scenario_comparison", "sensitivity_analysis",
      ].includes(normalizedCandidate)) {
        next.analysis_goal = normalizedCandidate as TaskSpec["analysis_goal"];
        applied = true;
      } else if (ambiguity.field === "analysis_goal" && goalAliases[normalizedCandidate]) {
        next.analysis_goal = goalAliases[normalizedCandidate]!;
        applied = true;
      } else if (ambiguity.field === "result_queries") {
        const metric = normalizedCandidate;
        if (["heat_exposure", "vulnerability", "access_disadvantage", "combined_disadvantage", "coverage_gap"].includes(metric)) {
          next.result_queries = [{ metric: metric as NonNullable<TaskSpec["result_queries"]>[number]["metric"], direction: "highest", limit: 5, scope: "all", raw_text: ambiguity.question }];
          applied = true;
        }
      }
      if (!applied) return current;
      const remaining = (current.ambiguities ?? []).filter((_, itemIndex) => itemIndex !== index);
      next.ambiguities = remaining;
      setDecision({
        disposition: remaining.length ? "needs_clarification" : (
          ["heat_review", "reachability_review", "tract_review", "minimum_facilities", "minimum_threshold"].includes(next.analysis_goal)
            ? "quick_diagnostic" : "full_analysis"
        ),
        reason: remaining.length
          ? "Another interpretation still needs confirmation."
          : "The model interpretation is ready for boundary and task confirmation.",
        alternative: remaining.map((item) => item.question).join(" "),
      });
      return next;
    });
  }

  return <Workbench activeTab={tab} onTabChange={setTab} onResult={showResults} place={task?.place_name} taskPending={interpreting || parseMutation.isPending}
    connection={configQuery.isError ? "unavailable" : configQuery.isSuccess ? "connected" : "connecting"}
    utciConfigured={configQuery.data?.utci_configured ?? configQuery.data?.sf_utci_configured}
    inputLayers={{
      boundary: (matchingArea ? liveInputs.data?.boundary : undefined) ?? selectedDemand?.boundary ?? confirmedPlace?.boundary ?? boundaryQuery.data,
      tract: (matchingArea ? liveInputs.data?.tract : undefined) ?? regionalData?.geometry,
      candidate: matchingRun ? liveInputs.data?.candidate : undefined,
    }}
    uploadedRun={matchingRun && snapshot?.task.candidate_source === "csv"}
    previewDraft={Boolean(snapshot && !matchingRun)}
    populationLayer={regionalData ? <div>
      {selectedDemand ? <span>{selectedDemand.filename}</span> : regionalQuery.data && <>
        <a href={regionalQuery.data.source_url} target="_blank" rel="noreferrer">{regionalQuery.data.source}</a>
        <small>{regionalQuery.data.notes}</small>
        <a href={`/api/v1/regional-data/download?place_name=${encodeURIComponent(regionalQuery.data.place_name)}`} download>{t("Download demand GeoJSON")}</a>
        <br />
        <a href={`/api/v1/regional-data/download?layer=boundary&place_name=${encodeURIComponent(regionalQuery.data.place_name)}`} download>{t("Download study boundary")}</a>
      </>}
      <small>{regionalData.units} {t("demand zones")}{regionalData.year ? ` · ${regionalData.year}` : ""}</small>
      <small>{(selectedDemand ? selectedDemand.fields.older : regionalQuery.data?.older_units) ? t("Total population and age 65+") : t("Total population only")}</small>
      <small>{t("Poverty and household vehicle availability are unavailable for this layer.")}</small>
    </div> : regionalPlace ? <div>
      <small>{t(regionalQuery.isFetching ? "Checking regional population and boundaries…" : regionalDataHelp)}</small>
      <a href={`/api/v1/regional-data/download?layer=boundary&place_name=${encodeURIComponent(regionalPlace)}`} download>{t("Download study boundary")}</a>
      {regionalQuery.error && <><small role="alert">{regionalQuery.error.message}</small>
        <button type="button" className="button--secondary" onClick={() => void regionalQuery.refetch()}>{t("Retry data check")}</button></>}
    </div> : undefined}
    settings={<LlmSettings defaults={configQuery.data?.agent as Record<string, unknown> | undefined} />}
    onUpload={(file) => void upload(file, candidateSourceCrs)}
    onDemandDrop={(file) => demandControl.current?.openFile(file)}
    uploadPending={uploadMutation.isPending}
    upload={<section className="workbench-upload-card">
      <h3><WorkspaceIcon name="upload" />{t("Use your own data")}</h3>
      <CandidateUpload pending={uploadMutation.isPending} uploadedName={uploadedName} onUpload={(file) => void upload(file, candidateSourceCrs)} onRemove={removeCandidateUpload} />
      <details className="workbench-disclosure candidate-coordinates">
        <summary>{t("Candidate coordinates")} · {candidateSourceCrs === "4326" ? "WGS84" : candidateSourceCrs === "gcj02" ? "GCJ-02" : `EPSG:${candidateSourceCrs}`}</summary>
        <select aria-label={t("Candidate coordinates")} value={candidateCrs} onChange={(event) => setCandidateCrs(event.target.value)} disabled={uploadMutation.isPending}>
          <option value="4326">WGS84 (EPSG:4326)</option>
          <option value="4490">CGCS2000 (EPSG:4490)</option>
          <option value="2326">Hong Kong 1980 Grid (EPSG:2326)</option>
          <option value="gcj02">{t("GCJ-02 (mainland China, approximate conversion)")}</option>
          <option value="custom">{t("Other EPSG code")}</option>
        </select>
        {candidateCrs === "custom" && <label className="field"><span>{t("Input EPSG code")}</span>
        <input type="number" min={1} value={candidateEpsg} onChange={(event) => setCandidateEpsg(event.target.value)} />
        </label>}
      </details>
      {uploadMutation.isError && <p className="error" role="alert">{uploadMutation.error.message}</p>}
      {candidateCrsError && <p className="error" role="alert">{candidateCrsError}</p>}
      <RegionalDataUpload ref={demandControl} place={task?.place_name} active={selectedDemand}
        onConfirm={confirmDemand} onRemove={removeDemandUpload} />
    </section>}
    chat={(runActivity, showUpload) =>
        <PromptInput audience={audience} autoDetect={autoDetect} onAutoDetectChange={setAutoDetect} pending={interpreting || (audience === "personal" ? residentBusy : parseMutation.isPending)}
          onSubmit={(prompt) => void submitPrompt(prompt)}
          onAudienceChange={(next) => void changeAudience(next)}
          onCancel={() => { void interruptWork().catch((reason: unknown) => setIntentError(reason instanceof Error ? reason.message : "Could not stop the previous task.")); }}
          onUseLocation={() => void handleUseLocation()} onShowUpload={showUpload} hasActiveRun={Boolean(activeRunId)}
          activity={<div className="workbench-chat__activity">
            {configQuery.isError && <div className="error error--block" role="alert"><p>Cannot reach the application backend. {configQuery.error.message}</p><button type="button" className="button--secondary" disabled={configQuery.isFetching} onClick={() => void configQuery.refetch()}>Retry connection</button></div>}
            {intentMessage && <p role="status" className="notice">{intentMessage}</p>}
            {intentError && <p role="alert" className="error">{intentError}</p>}
            {audience === "research" && decision && <div className={`request-decision request-decision--${decision.disposition}`} role="status"><strong>{decision.disposition.replaceAll("_", " ")}</strong><span>{decision.reason}</span>{decision.alternative && <span>{decision.alternative}</span>}</div>}
            {audience === "research" && error && <p className="error error--block" role="alert">{error instanceof Error ? error.message : "The request failed."}</p>}
            {runActivity}
          </div>}
        />}
    resident={audience === "personal" ? <ResidentWorkspace request={residentRequest} cancelVersion={residentCancel} onBusy={setResidentBusy} onPreferencesChange={rememberPreferences} onCancelReady={rememberResidentStop} /> : null}
    setup={task || resolution ? <div className="task-workspace">
      {task && routeOriginCandidates.length > 0 && (
        <details className="panel mobile-step-panel" data-study-area-step open={routeStepOpen} onToggle={(event) => setRouteStepOpen(event.currentTarget.open)}>
          <summary>
          <div className="section-heading">
            <div>
              <p className="step-heading"><span className="form-step">Step 2</span><span>Route origin</span></p>
              <h2>{t("Confirm the intended location")}</h2>
            </div>
            <span className={`status ${confirmedPlace ? "status--success" : "status--warning"}`}>
              {confirmedPlace ? "Origin selected" : "Confirmation required"}
            </span>
          </div>
          </summary>
          <div className="mobile-step-panel__body">
          <p className="muted">Choose an exact origin for route planning, or an administrative place to analyze as the study area.</p>
          <div className="candidate-list">
            {routeOriginCandidates.map((candidate) => (
              <button disabled={candidateSelectionLocked} className={`candidate-card ${selectedRouteOriginId === candidate.candidate_id ? "is-selected" : ""}`} aria-pressed={selectedRouteOriginId === candidate.candidate_id} key={candidate.candidate_id} type="button" onClick={() => void chooseRouteOrigin(candidate)}>
                <strong>{candidate.display_name}</strong>
                <span>{candidate.administrative ? "Use as study area" : "Use as route origin"} - {candidate.place_name}</span>
                {candidate.positional_hint && <small>Position described by user: {candidate.positional_hint}</small>}
              </button>
            ))}
          </div>
          {candidateSelectionLocked && <p className="notice">This draft is locked while its run is active. Cancel the run or analyze a new request before changing the origin.</p>}
          <form className="inline-form route-origin-refinement" onSubmit={refineRouteOrigin}>
            <label htmlFor="origin-refinement">Not listed? Enter a more specific landmark or address</label>
            <input id="origin-refinement" value={originRefinement} onChange={(event) => setOriginRefinement(event.target.value)} placeholder="For example, Walt Disney World, Florida" />
            <button type="submit" disabled={parseMutation.isPending || candidateSelectionLocked}>Search again</button>
          </form>
          </div>
        </details>
      )}

      {task && placeNameOptions.length > 1 && (
        <section className="panel" data-study-area-step>
          <div className="section-heading">
            <div>
              <p className="step-heading"><span className="form-step">Step 2</span><span>{t("Study area")}</span></p>
              <h2>{t("Choose one area for this run")}</h2>
            </div>
            <span className="status status--warning">{t("Confirmation required")}</span>
          </div>
          <p className="muted">Each area needs its own boundary, data lineage, travel matrix, and result. Saved runs can be compared afterward.</p>
          <div className="candidate-list">
            {placeNameOptions.map((placeName) => (
              <button className="candidate-card" key={placeName} type="button" onClick={() => void chooseStudyArea(placeName)}>
                <strong>{placeName}</strong>
                <span>Use as this run's study area</span>
              </button>
            ))}
          </div>
        </section>
      )}

      {task && !task.place_name && routeOriginCandidates.length === 0 && placeNameOptions.length === 0 && (
        <form className="panel inline-form" data-study-area-step onSubmit={submitManualPlace}>
          <label htmlFor="manual-place">{t("Enter a study area")}</label>
          <input id="manual-place" value={manualPlace} onChange={(event) => setManualPlace(event.target.value)} />
          <button type="submit" disabled={resolveMutation.isPending}>{t("Resolve place")}</button>
        </form>
      )}

      {resolution && (
        <PlaceConfirmation
          resolution={resolution}
          pending={confirmMutation.isPending}
          selectedCandidateId={confirmedPlace?.candidate_id ?? null}
          onConfirm={(candidateId) => void confirm(candidateId)}
        />
      )}
      {task?.place_name && !confirmedPlace && resolveMutation.isError && (
        <div className="panel">
          <p>{t("If an automatic study layer is unavailable, confirm your own boundary and population with a demand GeoJSON.")}</p>
          <button type="button" className="button--secondary" onClick={() => setTab("Data")}>{t("Upload demand data")}</button>
        </div>
      )}
      {regionalPlace && !selectedDemand && regionalQuery.isError && (
        <div className="notice">
          <p>{t(regionalDataHelp)}</p>
          <button type="button" className="button--secondary" onClick={() => setTab("Data")}>{t("Upload demand data")}</button>
        </div>
      )}

      {task && (
        <TaskForm
          task={task}
          mode={mode}
          ready={Boolean(confirmedPlace)}
          pending={startMutation.isPending || interpreting}
          uploadPending={uploadMutation.isPending}
          uploadedName={uploadedName}
          candidateUploadId={uploadedPointCount > 0 ? candidateUploadId : null}
          profiles={profiles}
          onResolveAmbiguity={resolveAmbiguity}
          onModeChange={setMode}
          onUpload={(file) => void upload(file, candidateSourceCrs)}
          onRemoveUpload={removeCandidateUpload}
          onSubmit={run}
        />
      )}
    </div> : null}
  />;
}
