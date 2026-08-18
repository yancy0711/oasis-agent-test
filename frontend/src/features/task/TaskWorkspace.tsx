import { useRef, useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import type { PlaceResolveResponse, RouteOriginCandidate, RoutingMode, TaskSpec } from "../../api/models";
import type { RequestDisposition } from "../../api/models";
import { getConfig } from "../../api/client";
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
  const [uploadedName, setUploadedName] = useState<string | null>(null);
  const [decision, setDecision] = useState<{ disposition: RequestDisposition; reason: string; alternative: string } | null>(null);
  const [draftLocked, setDraftLocked] = useState(false);
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
    const value = await resolveMutation.mutateAsync(name);
    setResolution(value);
    return value;
  }

  async function analyze(prompt: string) {
    parseController.current?.abort();
    const controller = new AbortController();
    parseController.current = controller;
    setConfirmedPlace(null);
    setResolution(null);
    setRouteOriginCandidates([]);
    setSelectedRouteOriginId(null);
    setDraftLocked(false);
    try {
      const parsed = await parseMutation.mutateAsync({ prompt, signal: controller.signal });
      if (controller.signal.aborted) return;
      setDecision({ disposition: parsed.disposition, reason: parsed.reason, alternative: parsed.alternative });
      setRouteOriginCandidates(parsed.route_origin_candidates ?? []);
      if (!parsed.task) { setTask(null); return; }
      const nextTask = taskFromDraft(parsed.task);
      setTask(nextTask);
      if (nextTask.place_name && !(parsed.route_origin_candidates?.length)) await resolve(nextTask.place_name);
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
      },
    } : current);
    const resolved = await resolve(candidate.place_name);
    if (resolved.selected) {
      await confirmResolution(resolved, resolved.selected.candidate_id);
      setResolution(null);
    }
  }

  function refineRouteOrigin(event: FormEvent) {
    event.preventDefault();
    const value = originRefinement.trim();
    if (value) void analyze(`I'm at ${value}. How can I find the nearest cooling facility?`);
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

  return (
    <div className="task-workspace">
      <section className="panel panel--hero">
        <p className="eyebrow">Policy and research decision support</p>
        <h1>OASIS: A bounded LLM agent for auditable, equity-aware cooling center siting</h1>
        <LlmSettings defaults={configQuery.data?.agent as Record<string, unknown> | undefined} />
        <PromptInput pending={parseMutation.isPending} onSubmit={(prompt) => void analyze(prompt)} onCancel={cancelAnalysis} />
        {decision && <div className={`request-decision request-decision--${decision.disposition}`} role="status"><strong>{decision.disposition.replaceAll("_", " ")}</strong><span>{decision.reason}</span>{decision.alternative && <span>{decision.alternative}</span>}</div>}
        {error && <p className="error error--block">{error instanceof Error ? error.message : "The request failed."}</p>}
      </section>

      {task && routeOriginCandidates.length > 0 && (
        <section className="panel">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Step 2 - Route origin</p>
              <h2>Confirm the intended location</h2>
            </div>
            <span className={`status ${confirmedPlace ? "status--success" : "status--warning"}`}>
              {confirmedPlace ? "Origin selected" : "Confirmation required"}
            </span>
          </div>
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
        </section>
      )}

      {task && !task.place_name && routeOriginCandidates.length === 0 && (
        <form className="panel inline-form" onSubmit={submitManualPlace}>
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
          onModeChange={setMode}
          onUpload={(file) => void upload(file)}
          onSubmit={run}
        />
      )}
    </div>
  );
}
