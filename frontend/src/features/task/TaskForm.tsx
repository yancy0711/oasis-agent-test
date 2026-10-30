import { zodResolver } from "@hookform/resolvers/zod";
import { useEffect, useState } from "react";
import { Controller, useForm, type Control } from "react-hook-form";
import type { RoutingMode, TaskSpec } from "../../api/models";
import { InfoLabel } from "../../components/InfoLabel";
import { LoadingText } from "../../components/LoadingText";
import { CheckboxDropdown } from "../../components/CheckboxDropdown";
import { CandidateUpload } from "./CandidateUpload";
import { useMediaQuery } from "../../hooks/useMediaQuery";
import { useTaskEditor, type TaskEditorPatch } from "./TaskEditorContext";
import { toTaskFormValues } from "./taskForm.mapper";
import { taskFormSchema, type TaskFormValues } from "./taskForm.schema";

type Weights = TaskFormValues["weights"];
export interface ValueProfile { name: string; label: string; weights: Weights }

interface TaskFormProps {
  task: TaskSpec; mode: RoutingMode; ready: boolean; pending: boolean; uploadPending: boolean;
  uploadedName: string | null; profiles: ValueProfile[];
  onModeChange: (mode: RoutingMode) => void; onUpload: (file: File) => void;
  onResolveAmbiguity?: (index: number, candidate: string) => void;
  onSubmit: (values: TaskFormValues) => void;
}

function NumberListEditor({ value, onChange, integer = false }: {
  value: number[]; onChange: (value: number[]) => void; integer?: boolean;
}) {
  const [draft, setDraft] = useState("");
  function add() {
    const parsed = Number(draft);
    if (Number.isFinite(parsed) && parsed > 0 && (!integer || Number.isInteger(parsed)) && !value.includes(parsed)) {
      onChange([...value, parsed].sort((a, b) => a - b)); setDraft("");
    }
  }
  return <div className="number-list">
    <div className="chip-list">{value.map((item) => <button type="button" className="chip" key={item} onClick={() => onChange(value.filter((entry) => entry !== item))}>{item} ×</button>)}</div>
    <div className="inline-control"><input type="number" min="1" step={integer ? 1 : 0.5} value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); add(); } }} /><button type="button" onClick={add}>Add</button></div>
  </div>;
}

function WeightField({ name, label, weights, control }: {
  name: keyof Weights; label: string; weights: Weights; control: Control<TaskFormValues>;
}) {
  const others = Object.entries(weights).reduce((sum, [key, value]) => key === name ? sum : sum + (Number(value) || 0), 0);
  const maximum = Math.max(0, Number((1 - others).toFixed(10)));
  return <Controller control={control} name={`weights.${name}`} render={({ field }) => <label className="field"><span>{label}</span><input {...field} type="number" min="0" max={maximum} step="0.05" onChange={(event) => {
    const next = Number(event.target.value);
    if (Number.isFinite(next) && next >= 0 && next <= maximum + 1e-9) field.onChange(next);
  }} /></label>} />;
}

function CoverageNumber({ label, value, calculated, integer = false, onChange }: {
  label: string; value: number; calculated: boolean; integer?: boolean; onChange: (value: number | null) => void;
}) {
  const values = [...new Set([value, ...(integer ? [1, 3, 5, 10, 15, 20, 30, 50, 100] : [5, 10, 12, 15, 20, 30, 45, 60, 90])])].sort((a, b) => a - b);
  return <CheckboxDropdown ariaLabel={label} single selected={[calculated ? "calculate" : String(value)]}
    options={[{ value: "calculate", label: "To calculate" }, ...values.map((number) => ({ value: String(number), label: String(number) }))]}
    summary={calculated ? "To calculate" : String(value)} onChange={(selected) => onChange(selected[0] === "calculate" ? null : Number(selected[0]))}
    footer={<label className="field"><span>Custom value</span><input aria-label={`Custom ${label}`} type="number" min={integer ? 1 : 0.1} max={integer ? 100 : undefined} step={integer ? 1 : "any"} value={calculated ? "" : value} placeholder="Enter a value" onChange={(event) => onChange(event.target.value === "" ? null : Number(event.target.value))} /></label>} />;
}

const AMENITY_OPTIONS = [
  { value: "library", label: "Public library" },
  { value: "community_centre", label: "Community center" },
  { value: "school", label: "School" },
  { value: "sports_centre", label: "Sports center" },
  { value: "arts_centre", label: "Arts center" },
  { value: "place_of_worship", label: "Place of worship" },
] as const;
const DEFAULT_AMENITIES = AMENITY_OPTIONS.map((option) => option.value);

export function TaskForm({ task, mode, ready, pending, uploadPending, uploadedName, profiles, onModeChange, onUpload, onResolveAmbiguity, onSubmit }: TaskFormProps) {
  const { register, handleSubmit, reset, watch, control, setValue, formState: { errors } } = useForm<TaskFormValues>({
    resolver: zodResolver(taskFormSchema), defaultValues: toTaskFormValues(task),
  });
  const editor = useTaskEditor();
  const mobile = useMediaQuery("(max-width: 620px)");
  const routeFocused = Boolean(task.route_origin);
  const [stepOpen, setStepOpen] = useState(true);
  const [settingsOpen, setSettingsOpen] = useState(!mobile);
  useEffect(() => reset(toTaskFormValues(task)), [task, reset]);
  useEffect(() => {
    setStepOpen(true);
    setSettingsOpen(!mobile);
  }, [mobile, routeFocused, task.analysis_goal, task.place_name, task.route_origin?.raw_text]);
  useEffect(() => {
    const apply = (patch: TaskEditorPatch) => Object.entries(patch).forEach(([key, value]) => setValue(key as keyof TaskFormValues, value as never, { shouldDirty: true, shouldValidate: true }));
    editor.register(apply); return () => editor.register(null);
  }, [editor, setValue]);

  const source = watch("candidate_source");
  const weights = watch("weights");
  const weightTotal = Object.values(weights).reduce((sum, value) => sum + (Number(value) || 0), 0);
  const hasTarget = watch("coverage_target_enabled");
  const hasCapacity = watch("facility_capacity_enabled");
  const hasEquityAlpha = watch("equity_alpha_enabled");
  const sensitivity = task.analysis_goal === "sensitivity_analysis";
  const comparison = task.analysis_goal === "scenario_comparison";
  const heatOnly = task.analysis_goal === "heat_review";
  const coverageControls = hasTarget;
  const fixedInputs = watch("diagnostic_fixed_inputs");
  const fixedBudget = !coverageControls || fixedInputs.includes("n_facilities");
  const fixedTime = !coverageControls || fixedInputs.includes("threshold_minutes");
  const canUseHypothetical = ["facility_siting", "minimum_facilities", "minimum_threshold"].includes(task.analysis_goal)
    && !task.result_queries?.length && fixedTime && (fixedBudget || hasTarget);
  const hypotheticalBudget = canUseHypothetical && fixedBudget && watch("allow_hypothetical_sites");
  const facilityCount = watch("n_facilities");
  const threshold = watch("threshold_minutes");
  const mobility = watch("mobility_scenario");
  const allProfiles = profiles;
  const appliedProfile = allProfiles.find((profile) => Object.entries(profile.weights).every(([key, value]) =>
    Math.abs(Number(weights[key as keyof typeof weights]) - value) < 0.000001));
  const recommended = profiles.find((profile) => profile.name === task.recommended_value_profile);
  function changeCoverageInput(field: "n_facilities" | "threshold_minutes", value: number | null) {
    if (value !== null) setValue(field, value, { shouldDirty: true, shouldValidate: true });
    setValue("diagnostic_fixed_inputs", value === null ? fixedInputs.filter((item) => item !== field) : [...new Set([...fixedInputs, field])], { shouldDirty: true, shouldValidate: true });
  }

  function applyProfile(profile: ValueProfile | undefined) { if (profile) setValue("weights", profile.weights, { shouldDirty: true, shouldValidate: true }); }
  function applyRecommendations() {
    if (task.recommended_mobility_scenario) setValue("mobility_scenario", task.recommended_mobility_scenario, { shouldDirty: true });
    applyProfile(recommended);
    if (task.recommended_sensitivity_thresholds?.length) setValue("sensitivity_thresholds", task.recommended_sensitivity_thresholds, { shouldDirty: true, shouldValidate: true });
    if (task.recommended_sensitivity_facility_counts?.length) setValue("sensitivity_facility_counts", task.recommended_sensitivity_facility_counts, { shouldDirty: true, shouldValidate: true });
  }

  return <form className="panel" onSubmit={handleSubmit(onSubmit)}>
    <details className="mobile-step-disclosure task-step-disclosure" open={stepOpen} onToggle={(event) => setStepOpen(event.currentTarget.open)}>
    <summary><div className="section-heading"><div><p className="step-heading"><span className="form-step">Step 3</span><span>Task settings</span></p><h2>Review the executable task</h2></div><span className="status">{task.analysis_goal.replaceAll("_", " ")}</span></div></summary>
    <div className="mobile-step-body">

    {(task.recommended_value_profile || task.recommended_mobility_scenario || task.recommended_sensitivity_thresholds?.length) ? <div className="recommendation-panel">
      <div><strong>Model recommendation</strong><p>{task.recommendation_rationale || "Review and explicitly apply the suggested settings."}</p></div>
      {recommended && <p>Suggested value profile: <strong>{recommended.label}</strong>. {appliedProfile?.name === recommended.name ? "Applied to the displayed weights." : "Not applied yet; review and apply before running."}</p>}
      <button type="button" onClick={applyRecommendations}>Apply all recommendations</button>
    </div> : null}
    {task.route_origin && <div className="route-request-notice"><strong>Route origin</strong><span>{task.route_origin.entity}{task.route_origin.context ? `, ${task.route_origin.context}` : ""}</span>{task.route_origin.positional_hint && <small>Position described by user: {task.route_origin.positional_hint}. The confirmed candidate supplies the route coordinate.</small>}<small>Confirm the inferred city or county and run the candidate selection. The Map tab will then plan to the network-nearest selected facility.</small></div>}
    {task.preference_question && <div className="preference-panel"><strong>{task.preference_question}</strong><div className="button-row">{allProfiles.map((profile) => <button type="button" key={profile.name} onClick={() => applyProfile(profile)}>{profile.label}</button>)}</div></div>}
    {(task.result_queries?.length ?? 0) > 0 && <div className="route-request-notice"><strong>Requested result questions</strong>{task.result_queries?.map((query, index) => <span key={`${query.metric}-${index}`}>{query.limit} {query.scope === "vulnerable" ? "vulnerable " : ""}tract(s) by {query.metric.replaceAll("_", " ")} ({query.direction})</span>)}<small>These answers are read-only outputs and do not change the facility budget.</small></div>}
    {(task.ambiguities?.length ?? 0) > 0 && <div className="preference-panel"><strong>Confirm the model interpretation</strong>{task.ambiguities?.map((item, index) => { const candidates = item.candidates ?? []; return <div className="ambiguity-choice" key={`${item.field}-${index}`}><p>{item.question}</p>{candidates.length > 0 ? <div className="button-row">{candidates.map((candidate) => <button type="button" key={candidate} onClick={() => onResolveAmbiguity?.(index, candidate)}>{candidate.replaceAll("_", " ")}</button>)}</div> : <small>Edit the corresponding field below, then run the confirmed task.</small>}</div>; })}</div>}

    {routeFocused && <div className="route-essential-settings"><label className="field"><InfoLabel label="Travel mode">Used by the point route planner after candidate facilities have been prepared.</InfoLabel><select aria-label="Mobility" {...register("mobility_scenario")}><option value="walking">Walking</option><option value="cycling">Cycling</option><option value="driving">Driving</option></select></label><p>Supporting defaults: {facilityCount} candidate facilities, {threshold} minute planning threshold, and all supported facility types.</p></div>}

    <details className="task-settings-disclosure" open={settingsOpen} onToggle={(event) => setSettingsOpen(event.currentTarget.open)}>
      <summary><span>{routeFocused ? "Customize supporting analysis" : "Analysis settings"}</span><small>{heatOnly ? "Heat diagnostic" : `${fixedBudget ? `${facilityCount} facilities` : "Facilities to calculate"} - ${fixedTime ? `${threshold} min` : "Time to calculate"} - ${mobility.replaceAll("_", " ")}`}</small></summary>
      <div className="task-settings-body">
    {!heatOnly && <>
      <div className="form-grid">
        <div className="field"><span>Facilities</span>{coverageControls ? <CoverageNumber label="Facilities" value={facilityCount} calculated={!fixedBudget} integer onChange={(value) => changeCoverageInput("n_facilities", value)} /> : <input aria-label="Facilities" type="number" {...register("n_facilities", { valueAsNumber: true })} />}{errors.n_facilities && <small className="error">{errors.n_facilities.message}</small>}</div>
        <div className="field"><span>Threshold (minutes)</span>{coverageControls ? <CoverageNumber label="Threshold (minutes)" value={threshold} calculated={!fixedTime} onChange={(value) => changeCoverageInput("threshold_minutes", value)} /> : <input aria-label="Threshold (minutes)" type="number" step="any" {...register("threshold_minutes", { valueAsNumber: true })} />}{errors.threshold_minutes && <small className="error">{errors.threshold_minutes.message}</small>}</div>
        {errors.diagnostic_fixed_inputs && <small className="error">{errors.diagnostic_fixed_inputs.message}</small>}
        <label className="field"><InfoLabel label="Facility capacity">Limits how many people each selected candidate facility can be assigned. Leave disabled when reliable capacity data are unavailable.</InfoLabel><span className="check-row"><input type="checkbox" {...register("facility_capacity_enabled")} />Apply capacity</span>{hasCapacity && <input aria-label="People per facility" type="number" min="1" step="1" {...register("facility_capacity", { valueAsNumber: true })} />}</label>
        <label className="field"><InfoLabel label="Efficiency–equality trade-off">When enabled, selects the final recommendation from the computed efficiency–Gini frontier. α = 0 emphasizes average gradual access; α = 1 emphasizes equal access. This cannot be combined with a hard coverage target.</InfoLabel><span className="check-row"><input type="checkbox" {...register("equity_alpha_enabled")} />Select final solution by α</span>{hasEquityAlpha && <><input aria-label="Equity alpha" type="range" min="0" max="1" step="0.1" {...register("equity_alpha", { valueAsNumber: true })} /><small>α = {watch("equity_alpha").toFixed(1)} · 0 means efficiency, 1 means equality</small>{errors.equity_alpha && <small className="error">{errors.equity_alpha.message}</small>}</>}</label>
        {!routeFocused && <label className="field"><InfoLabel label="Mobility">Chooses the travel mode used to calculate access times between demand and candidate facilities.</InfoLabel><select aria-label="Mobility" {...register("mobility_scenario")}><option value="walking">Walking</option><option value="cycling">Cycling</option><option value="driving">Driving</option><option value="walk_drive_equity">Walk/drive equity</option></select><small>{watch("mobility_scenario") === "walk_drive_equity" ? "Evaluates access across walking and driving populations." : "Used for travel-time routing."}</small></label>}
        <label className="field"><InfoLabel label="Execution">Controls routing fidelity. Fixed-budget hypothetical planning is approximate only and uses Quick without ORS refinement.</InfoLabel><select aria-label="Execution" disabled={hypotheticalBudget} value={hypotheticalBudget ? "quick" : mode} onChange={(event) => onModeChange(event.target.value as RoutingMode)}><option value="auto">Auto preview + precise</option><option value="quick">Quick approximate</option><option value="precise">Precise network</option></select></label>
        <label className="field"><InfoLabel label="Candidate source">Chooses whether possible facility locations come from OpenStreetMap or an uploaded CSV.</InfoLabel><select aria-label="Candidate source" {...register("candidate_source")}><option value="osm">OpenStreetMap</option><option value="csv">Uploaded CSV</option></select></label>
        <label className="field"><InfoLabel label="Coverage group">Chooses whether coverage requirements apply to every tract or only the configured composite vulnerability priority tracts.</InfoLabel><select aria-label="Coverage group" {...register("coverage_group")}><option value="all">All tracts</option><option value="vulnerable">Vulnerability priority tracts</option></select></label>
        {source === "osm" && <Controller control={control} name="candidate_amenities" render={({ field }) => {
          const labels = AMENITY_OPTIONS.filter((option) => field.value.includes(option.value)).map((option) => option.label);
          return <div className="field field--wide"><InfoLabel label="OpenStreetMap candidate facility types">Choose which facility types may become candidate facilities. This list contains every type currently supported by the OASIS OpenStreetMap query; individual facilities are discovered inside the study area when the run starts.</InfoLabel><CheckboxDropdown ariaLabel="OpenStreetMap candidate facility types" options={AMENITY_OPTIONS} selected={field.value} onChange={field.onChange} summary={`${field.value.length} of ${AMENITY_OPTIONS.length} types selected`} detail={labels.length ? labels.join(", ") : "Select at least one type"} actions={<><button type="button" className="button--secondary" onClick={() => field.onChange(AMENITY_OPTIONS.map((option) => option.value))}>Select all</button><button type="button" className="button--secondary" onClick={() => field.onChange(DEFAULT_AMENITIES)}>Restore defaults</button></>} />{errors.candidate_amenities && <small className="error">{errors.candidate_amenities.message}</small>}</div>;
        }} />}
        {source === "csv" && <CandidateUpload pending={uploadPending} uploadedName={uploadedName} onUpload={onUpload} />}
      </div>
      <label className="check-row"><input type="checkbox" {...register("coverage_target_enabled")} /><InfoLabel label="Require a coverage target">Adds a minimum share of the selected population group that the solution must cover within the travel-time threshold.</InfoLabel></label>
      {hasTarget && <label className="field compact-field"><span>Coverage target (0–1)</span><input type="number" min="0" max="1" step="any" {...register("coverage_target", { valueAsNumber: true })} /></label>}
      {comparison && <Controller control={control} name="comparison_profiles" render={({ field }) => {
        const options = profiles.map((profile) => ({ value: profile.name, label: profile.label }));
        const labels = profiles.filter((profile) => field.value.includes(profile.name)).map((profile) => profile.label);
        return <div className="comparison-profile-picker field field--wide"><InfoLabel label="Value profiles to compare">Each selected profile supplies a different audited weighting scheme. Every profile is solved against the same candidate facilities and travel matrix.</InfoLabel><CheckboxDropdown ariaLabel="Value profiles to compare" options={options} selected={field.value} onChange={field.onChange} summary={`${field.value.length} profiles selected`} detail={labels.length ? labels.join(", ") : "Select at least two profiles"} actions={<><button type="button" className="button--secondary" onClick={() => field.onChange(options.map((option) => option.value))}>Select all</button><button type="button" className="button--secondary" onClick={() => field.onChange(["efficiency_first", "equity_first"].filter((name) => options.some((option) => option.value === name)))}>Efficiency vs equity</button></>} emptyMessage="No registered profiles are available." defaultOpen />{errors.comparison_profiles && <small className="error">{errors.comparison_profiles.message}</small>}</div>;
      }} />}
      <fieldset className={`weights ${routeFocused ? "weights--secondary" : ""}`}><legend><InfoLabel label="Value profile">Sets how strongly population, older adults, poverty, heat exposure, and passive built-environment burden influence site selection.</InfoLabel></legend><label className="field compact-field"><InfoLabel label="Apply a registered profile">Loads a saved combination of value weights; choose Custom weights to edit them manually.</InfoLabel><select aria-label="Apply a registered profile" value={appliedProfile?.name ?? ""} onChange={(event) => applyProfile(allProfiles.find((profile) => profile.name === event.target.value))}><option value="">Custom weights</option>{allProfiles.map((profile) => <option key={profile.name} value={profile.name}>{profile.label}</option>)}</select></label><div className="form-grid form-grid--weights">
        <WeightField name="population_density" label="Population" weights={weights} control={control} /><WeightField name="elderly_ratio" label="Older adults" weights={weights} control={control} /><WeightField name="poverty_rate" label="Poverty" weights={weights} control={control} /><WeightField name="heat_exposure" label="Heat exposure" weights={weights} control={control} /><WeightField name="environment_burden" label="Passive environment burden" weights={weights} control={control} />
      </div><div className={`weight-total ${Math.abs(weightTotal - 1) < .0001 ? "weight-total--ready" : "weight-total--warning"}`}><strong>Total: {weightTotal.toFixed(2)}</strong><span>{weightTotal < .9999 ? `Add ${(1 - weightTotal).toFixed(2)} before running.` : "Weights are ready."}</span></div>{errors.weights?.root && <small className="error">{errors.weights.root.message}</small>}</fieldset>
    </>}

    {!heatOnly && <fieldset className="weights"><legend>Extension</legend><label className="check-row"><input type="checkbox" aria-label="Allow hypothetical sites (extension)" disabled={!canUseHypothetical} {...register("allow_hypothetical_sites")} onChange={event => { setValue("allow_hypothetical_sites", event.target.checked, { shouldDirty: true, shouldValidate: true }); if (event.target.checked && fixedBudget) onModeChange("quick"); }} /><InfoLabel label="Allow hypothetical sites (extension)">Post-paper extension: add hypothetical sites at target tract demand centres. With a fixed budget, retain the budget, threshold, coverage target and value weights. With Facilities set to To calculate, minimize the count. Single travel mode; up to 250 target tracts. No capacity, regional, Pareto-alpha or extra ranking constraints. Travel is approximate; premises and access are unverified.</InfoLabel></label>{errors.allow_hypothetical_sites && <small className="error">{errors.allow_hypothetical_sites.message}</small>}</fieldset>}

    {sensitivity && <fieldset className="weights"><legend>Sensitivity grid</legend><div className="form-grid"><div className="field"><span>Thresholds (minutes)</span><Controller control={control} name="sensitivity_thresholds" render={({ field }) => <NumberListEditor value={field.value} onChange={field.onChange} />} />{errors.sensitivity_thresholds && <small className="error">{errors.sensitivity_thresholds.message}</small>}</div><div className="field"><span>Facility counts</span><Controller control={control} name="sensitivity_facility_counts" render={({ field }) => <NumberListEditor integer value={field.value} onChange={field.onChange} />} /></div></div><small>Maximum 12 values per list and 36 experiments total.</small></fieldset>}
      </div>
    </details>

    </div>
    </details>

    <div className="form-actions"><span>{ready ? "The confirmed task is ready to run." : "Confirm a study area before running."}</span><button type="submit" disabled={!ready || pending || (source === "csv" && !uploadedName)}>{pending ? <LoadingText>Starting</LoadingText> : "Run confirmed agent"}</button></div>
  </form>;
}
