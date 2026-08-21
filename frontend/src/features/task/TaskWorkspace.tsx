import { useRef, useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import type { PlaceResolveResponse, RouteOriginCandidate, RoutingMode, TaskSpec } from "../../api/models";
import type { RequestDisposition } from "../../api/models";
import { getConfig, reverseRouteOrigin, searchRouteOrigin } from "../../api/client";
import { configKeys } from "../../api/queryKeys";
import { useAppStore } from "../../store/appStore";
import { useStartRun } from "../run/run.mutations";
import { isTerminalRun } from "../run/run.selectors";
import { useRunPolling } from "../run/useRunPolling";
import { PlaceConfirmation } from "./PlaceConfirmation";
import { PromptInput } from "./PromptInput";
import { LlmSettings } from "./LlmSettings";
import { TaskForm, type ValueProfile } from "./TaskForm";
import { taskFromDraft, toTaskSpec } from "./taskForm.mapper";
import type { TaskFormValues } from "./taskForm.schema";
import {
  useConfirmPlace,
  useParseTask,
  useResolvePlace,
  useUploadCandidates,
} from "./task.mutations";

export function TaskWorkspace() {
  const [task, setTask] = useState<TaskSpec | null>(null);
  const [resolution, setResolution] = useState<PlaceResolveResponse | null>(null);
  const [mode, setMode] = useState<RoutingMode>("auto");
  const [manualPlace, setManualPlace] = useState("");
  const [originRefinement, setOriginRefinement] = useState("");
  const [selectedRouteOriginId, setSelectedRouteOriginId] = useState<string | null>(null);
  const [routeOriginCandidates, setRouteOriginCandidates] = useState<RouteOriginCandidate[]>([]);
  const [placeNameOptions, setPlaceNameOptions] = useState<string[]>([]);
  const [uploadedName, setUploadedName] = useState<string | null>(null);
  const [decision, setDecision] = useState<{ disposition: RequestDisposition; reason: string; alternative: string } | null>(null);
  const [draftLocked, setDraftLocked] = useState(false);
  const [routeStepOpen, setRouteStepOpen] = useState(true);
  const parseController = useRef<AbortController | null>(null);
  const confirmedPlace = useAppStore((state) => state.confirmedPlace);
  const candidateUploadId = useAppStore((state) => state.candidateUploadId);
  const activeRunId = useAppStore((state) => state.activeRunId);
  const setConfirmedPlace = useAppStore((state) => state.setConfirmedPlace);
  const setCandidateUploadId = useAppStore((state) => state.setCandidateUploadId);

  const parseMutation = useParseTask();
  const resolveMutation = useResolvePlace();
  const confirmMutation = useConfirmPlace();
  const uploadMutation = useUploadCandidates();
  const startMutation = useStartRun();
  const activeRunQuery = useRunPolling(activeRunId);
  const candidateSelectionLocked = draftLocked && Boolean(
    activeRunId && (!activeRunQuery.data || !isTerminalRun(activeRunQuery.data))
  );
  const configQuery = useQuery({ queryKey: configKeys.detail, queryFn: getConfig, staleTime: Infinity });

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
  const error = parseError ?? resolveMutation.error ?? confirmMutation.error
    ?? uploadMutation.error ?? startMutation.error;

  async function resolve(name: string) {
    try {
      const value = await resolveMutation.mutateAsync(name);
      setResolution(value);
      return value;
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason);
      if (message.includes("outside the United States") || message.includes("supports US city and county")) {
        resolveMutation.reset();
        setTask(null);
        setResolution(null);
        setDecision({
          disposition: "out_of_scope",
          reason: message,
          alternative: "Choose a city or county in the United States.",
        });
        return null;
      }
      throw reason;
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
      if (!parsed.task) { setDecision(parsedDecision); setTask(null); return; }
      const nextTask = taskFromDraft(parsed.task);
      setTask(nextTask);
      if (nextTask.place_name && !(parsed.route_origin_candidates?.length) && !(parsed.place_name_options?.length)) {
        setDecision({
          disposition: "needs_clarification",
          reason: "Checking that the study area is a supported US city or county.",
          alternative: "",
        });
        const resolved = await resolve(nextTask.place_name);
        if (resolved) setDecision(parsedDecision);
      } else setDecision(parsedDecision);
    } catch (reason) {
      if (!(reason instanceof DOMException && reason.name === "AbortError")) throw reason;
    } finally {
      if (parseController.current === controller) parseController.current = null;
    }
  }

  function cancelAnalysis() {
    parseController.current?.abort();
    parseController.current = null;
    parseMutation.reset();
  }

  async function confirmResolution(target: PlaceResolveResponse, candidateId: string) {
    const place = await confirmMutation.mutateAsync({
      resolutionId: target.resolution_id,
      candidateId,
    });
    setConfirmedPlace(place);
    setTask((current) => current ? { ...current, place_name: place.canonical_name } : current);
  }

  async function confirm(candidateId: string) {
    if (resolution) await confirmResolution(resolution, candidateId);
  }

  async function chooseStudyArea(placeName: string) {
    const remainingAmbiguities = task?.ambiguities?.filter((item) => item.field !== "place_names") ?? [];
    setConfirmedPlace(null);
    setResolution(null);
    setTask((current) => current ? {
      ...current, place_name: placeName, place_names: [placeName], ambiguities: remainingAmbiguities,
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

  function useCurrentLocation() {
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

  async function upload(file: File) {
    const result = await uploadMutation.mutateAsync(file);
    setUploadedName(result.filename);
    setCandidateUploadId(result.upload_id);
  }

  function run(values: TaskFormValues) {
    if (!task || !confirmedPlace) return;
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

  return (
    <div className="task-workspace">
      <section className="panel panel--hero">
        <p className="eyebrow">Policy and research decision support</p>
        <h1>OASIS: A bounded LLM agent for auditable, equity-aware cooling center siting</h1>
        <LlmSettings defaults={configQuery.data?.agent as Record<string, unknown> | undefined} />
        <PromptInput pending={parseMutation.isPending} onSubmit={(prompt) => void analyze(prompt)} onCancel={cancelAnalysis} onUseLocation={useCurrentLocation} />
        {decision && <div className={`request-decision request-decision--${decision.disposition}`} role="status"><strong>{decision.disposition.replaceAll("_", " ")}</strong><span>{decision.reason}</span>{decision.alternative && <span>{decision.alternative}</span>}</div>}
        {error && <p className="error error--block">{error instanceof Error ? error.message : "The request failed."}</p>}
      </section>

      {task && routeOriginCandidates.length > 0 && (
        <details className="panel mobile-step-panel" data-study-area-step open={routeStepOpen} onToggle={(event) => setRouteStepOpen(event.currentTarget.open)}>
          <summary>
          <div className="section-heading">
            <div>
              <p className="step-heading"><span className="form-step">Step 2</span><span>Route origin</span></p>
              <h2>Confirm the intended location</h2>
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
              <p className="step-heading"><span className="form-step">Step 2</span><span>Study area</span></p>
              <h2>Choose one area for this run</h2>
            </div>
            <span className="status status--warning">Confirmation required</span>
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
          <label htmlFor="manual-place">The request needs a US city or county</label>
          <input id="manual-place" value={manualPlace} onChange={(event) => setManualPlace(event.target.value)} />
          <button type="submit" disabled={resolveMutation.isPending}>Resolve place</button>
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

      {task && (
        <TaskForm
          task={task}
          mode={mode}
          ready={Boolean(confirmedPlace)}
          pending={startMutation.isPending}
          uploadPending={uploadMutation.isPending}
          uploadedName={uploadedName}
          profiles={profiles}
          onResolveAmbiguity={resolveAmbiguity}
          onModeChange={setMode}
          onUpload={(file) => void upload(file)}
          onSubmit={run}
        />
      )}
    </div>
  );
}
