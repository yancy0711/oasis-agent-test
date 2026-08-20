import { zodResolver } from "@hookform/resolvers/zod";
import { useEffect, useState } from "react";
import { Controller, useForm, type Control } from "react-hook-form";
import type { RoutingMode, TaskSpec } from "../../api/models";
import { InfoLabel } from "../../components/InfoLabel";
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

const QUICK_PROFILES: ValueProfile[] = [
  { name: "heat_poverty", label: "Heat + poverty", weights: { population_density: .1, elderly_ratio: .1, poverty_rate: .35, heat_exposure: .35, environment_burden: .1 } },
  { name: "older_adults", label: "Older adults", weights: { population_density: .1, elderly_ratio: .6, poverty_rate: .1, heat_exposure: .1, environment_burden: .1 } },
  { name: "balanced", label: "Balanced", weights: { population_density: .2, elderly_ratio: .2, poverty_rate: .2, heat_exposure: .2, environment_burden: .2 } },
];

const AMENITY_OPTIONS = [
  { value: "library", label: "Public library" },
  { value: "community_centre", label: "Community center" },
  { value: "school", label: "School" },
  { value: "sports_centre", label: "Sports center" },
  { value: "arts_centre", label: "Arts center" },
  { value: "place_of_worship", label: "Place of worship" },
] as const;
const DEFAULT_AMENITIES = AMENITY_OPTIONS.map((option) => option.value);

export function TaskForm({ task, mode, ready, pending, uploadPending, uploadedName, profiles, onModeChange, onUpload, onSubmit }: TaskFormProps) {
  const { register, handleSubmit, reset, watch, control, setValue, formState: { errors } } = useForm<TaskFormValues>({
    resolver: zodResolver(taskFormSchema), defaultValues: toTaskFormValues(task),
  });
  const editor = useTaskEditor();
  const mobile = useMediaQuery("(max-width: 620px)");
  const [mobileStepOpen, setMobileStepOpen] = useState(true);
  const [mobileSettingsOpen, setMobileSettingsOpen] = useState(false);
  useEffect(() => reset(toTaskFormValues(task)), [task, reset]);
  useEffect(() => {
    setMobileStepOpen(true);
    setMobileSettingsOpen(false);
  }, [task]);
  useEffect(() => {
    const apply = (patch: TaskEditorPatch) => Object.entries(patch).forEach(([key, value]) => setValue(key as keyof TaskFormValues, value as never, { shouldDirty: true, shouldValidate: true }));
    editor.register(apply); return () => editor.register(null);
  }, [editor, setValue]);

  const source = watch("candidate_source");
  const weights = watch("weights");
  const weightTotal = Object.values(weights).reduce((sum, value) => sum + (Number(value) || 0), 0);
  const hasWindow = watch("operating_window_enabled");
  const hasTarget = watch("coverage_target_enabled");
  const hasCapacity = watch("facility_capacity_enabled");
  const hasEquityAlpha = watch("equity_alpha_enabled");
  const sensitivity = task.analysis_goal === "sensitivity_analysis";
  const comparison = task.analysis_goal === "scenario_comparison";
  const heatOnly = task.analysis_goal === "heat_review";
  const facilityCount = watch("n_facilities");
  const threshold = watch("threshold_minutes");
  const mobility = watch("mobility_scenario");
  const allProfiles = [...profiles, ...QUICK_PROFILES.filter((quick) => !profiles.some((profile) => profile.name === quick.name))];
  const recommended = profiles.find((profile) => profile.name === task.recommended_value_profile);

  function applyProfile(profile: ValueProfile | undefined) { if (profile) setValue("weights", profile.weights, { shouldDirty: true, shouldValidate: true }); }
  function applyRecommendations() {
    if (task.recommended_mobility_scenario) setValue("mobility_scenario", task.recommended_mobility_scenario, { shouldDirty: true });
    applyProfile(recommended);
    if (task.recommended_sensitivity_thresholds?.length) setValue("sensitivity_thresholds", task.recommended_sensitivity_thresholds, { shouldDirty: true, shouldValidate: true });
    if (task.recommended_sensitivity_facility_counts?.length) setValue("sensitivity_facility_counts", task.recommended_sensitivity_facility_counts, { shouldDirty: true, shouldValidate: true });
  }

  return <form className="panel" onSubmit={handleSubmit(onSubmit)}>
    <details className="mobile-step-disclosure task-step-disclosure" open={!mobile || mobileStepOpen} onToggle={(event) => {
      if (mobile) setMobileStepOpen(event.currentTarget.open);
    }}>
    <summary><span><span className="form-step">Step 3</span> Review the executable task</span></summary>
    <div className="mobile-step-body">
    <div className="section-heading task-step-heading"><div><p className="eyebrow">Step 3 - Human confirmation</p><h2>Review the executable task</h2></div><span className="status">{task.analysis_goal.replaceAll("_", " ")}</span></div>

    {(task.recommended_value_profile || task.recommended_mobility_scenario || task.recommended_sensitivity_thresholds?.length) ? <div className="recommendation-panel">
      <div><strong>Model recommendation</strong><p>{task.recommendation_rationale || "Review and explicitly apply the suggested settings."}</p></div>
      <button type="button" onClick={applyRecommendations}>Apply all recommendations</button>
    </div> : null}
    {task.route_origin && <div className="route-request-notice"><strong>Route origin</strong><span>{task.route_origin.entity}{task.route_origin.context ? `, ${task.route_origin.context}` : ""}</span>{task.route_origin.positional_hint && <small>Position described by user: {task.route_origin.positional_hint}. The confirmed candidate supplies the route coordinate.</small>}<small>Confirm the inferred city or county and run the candidate selection. The Map tab will then plan to the network-nearest selected facility.</small></div>}
    {task.preference_question && <div className="preference-panel"><strong>{task.preference_question}</strong><div className="button-row">{QUICK_PROFILES.map((profile) => <button type="button" key={profile.name} onClick={() => applyProfile(profile)}>{profile.label}</button>)}</div></div>}

    <details className="task-settings-disclosure" open={!mobile || mobileSettingsOpen} onToggle={(event) => {
      if (mobile) setMobileSettingsOpen(event.currentTarget.open);
    }}>
      <summary><span>Analysis settings</span><small>{heatOnly ? "Heat diagnostic" : `${facilityCount} facilities - ${threshold} min - ${mobility.replaceAll("_", " ")}`}</small></summary>
      <div className="task-settings-body">
    {!heatOnly && <>
      <div className="form-grid">
        <label className="field"><span>Facilities</span><input type="number" {...register("n_facilities", { valueAsNumber: true })} />{errors.n_facilities && <small className="error">{errors.n_facilities.message}</small>}</label>
        <label className="field"><span>Threshold (minutes)</span><input type="number" step="0.5" {...register("threshold_minutes", { valueAsNumber: true })} />{errors.threshold_minutes && <small className="error">{errors.threshold_minutes.message}</small>}</label>
        <label className="field"><InfoLabel label="Facility capacity">Limits how many people each selected candidate facility can be assigned. Leave disabled when reliable capacity data are unavailable.</InfoLabel><span className="check-row"><input type="checkbox" {...register("facility_capacity_enabled")} />Apply capacity</span>{hasCapacity && <input aria-label="People per facility" type="number" min="1" step="1" {...register("facility_capacity", { valueAsNumber: true })} />}</label>
        <label className="field"><InfoLabel label="Efficiency–equality trade-off">When enabled, selects the final recommendation from the computed efficiency–Gini frontier. α = 0 emphasizes average gradual access; α = 1 emphasizes equal access. This cannot be combined with a hard coverage target.</InfoLabel><span className="check-row"><input type="checkbox" {...register("equity_alpha_enabled")} />Select final solution by α</span>{hasEquityAlpha && <><input aria-label="Equity alpha" type="range" min="0" max="1" step="0.1" {...register("equity_alpha", { valueAsNumber: true })} /><small>α = {watch("equity_alpha").toFixed(1)} · 0 means efficiency, 1 means equality</small>{errors.equity_alpha && <small className="error">{errors.equity_alpha.message}</small>}</>}</label>
        <label className="field"><InfoLabel label="Mobility">Chooses the travel mode used to calculate access times between demand and candidate facilities.</InfoLabel><select aria-label="Mobility" {...register("mobility_scenario")}><option value="walking">Walking</option><option value="cycling">Cycling</option><option value="driving">Driving</option><option value="walk_drive_equity">Walk/drive equity</option></select><small>{watch("mobility_scenario") === "walk_drive_equity" ? "Evaluates access across walking and driving populations." : "Used for travel-time routing."}</small></label>
        <label className="field"><InfoLabel label="Execution">Controls routing fidelity: quick is approximate, precise uses the network router, and auto runs preview before precise.</InfoLabel><select aria-label="Execution" value={mode} onChange={(event) => onModeChange(event.target.value as RoutingMode)}><option value="auto">Auto preview + precise</option><option value="quick">Quick approximate</option><option value="precise">Precise network</option></select></label>
        <label className="field"><InfoLabel label="Candidate source">Chooses whether possible facility locations come from OpenStreetMap or an uploaded CSV.</InfoLabel><select aria-label="Candidate source" {...register("candidate_source")}><option value="osm">OpenStreetMap</option><option value="csv">Uploaded CSV</option></select></label>
        <label className="field"><InfoLabel label="Coverage group">Chooses whether coverage requirements apply to every tract or only the configured composite vulnerability priority tracts.</InfoLabel><select aria-label="Coverage group" {...register("coverage_group")}><option value="all">All tracts</option><option value="vulnerable">Vulnerability priority tracts</option></select></label>
        {source === "osm" && <Controller control={control} name="candidate_amenities" render={({ field }) => {
          const labels = AMENITY_OPTIONS.filter((option) => field.value.includes(option.value)).map((option) => option.label);
          return <div className="field field--wide"><InfoLabel label="OpenStreetMap candidate facility types">Choose which facility types may become candidate facilities. This list contains every type currently supported by the OASIS OpenStreetMap query; individual facilities are discovered inside the study area when the run starts.</InfoLabel><CheckboxDropdown ariaLabel="OpenStreetMap candidate facility types" options={AMENITY_OPTIONS} selected={field.value} onChange={field.onChange} summary={`${field.value.length} of ${AMENITY_OPTIONS.length} types selected`} detail={labels.length ? labels.join(", ") : "Select at least one type"} actions={<><button type="button" className="button--secondary" onClick={() => field.onChange(AMENITY_OPTIONS.map((option) => option.value))}>Select all</button><button type="button" className="button--secondary" onClick={() => field.onChange(DEFAULT_AMENITIES)}>Restore defaults</button></>} />{errors.candidate_amenities && <small className="error">{errors.candidate_amenities.message}</small>}</div>;
        }} />}
        {source === "csv" && <CandidateUpload pending={uploadPending} uploadedName={uploadedName} onUpload={onUpload} />}
      </div>
      <label className="check-row"><input type="checkbox" {...register("coverage_target_enabled")} /><InfoLabel label="Require a coverage target">Adds a minimum share of the selected population group that the solution must cover within the travel-time threshold.</InfoLabel></label>
      {hasTarget && <label className="field compact-field"><span>Coverage target (0–1)</span><input type="number" min="0" max="1" step="0.05" {...register("coverage_target", { valueAsNumber: true })} /></label>}
      {comparison && <Controller control={control} name="comparison_profiles" render={({ field }) => {
        const options = profiles.map((profile) => ({ value: profile.name, label: profile.label }));
        const labels = profiles.filter((profile) => field.value.includes(profile.name)).map((profile) => profile.label);
        return <div className="comparison-profile-picker field field--wide"><InfoLabel label="Value profiles to compare">Each selected profile supplies a different audited weighting scheme. Every profile is solved against the same candidate facilities and travel matrix.</InfoLabel><CheckboxDropdown ariaLabel="Value profiles to compare" options={options} selected={field.value} onChange={field.onChange} summary={`${field.value.length} profiles selected`} detail={labels.length ? labels.join(", ") : "Select at least two profiles"} actions={<><button type="button" className="button--secondary" onClick={() => field.onChange(options.map((option) => option.value))}>Select all</button><button type="button" className="button--secondary" onClick={() => field.onChange(["efficiency_first", "equity_first"].filter((name) => options.some((option) => option.value === name)))}>Efficiency vs equity</button></>} emptyMessage="No registered profiles are available." defaultOpen />{errors.comparison_profiles && <small className="error">{errors.comparison_profiles.message}</small>}</div>;
      }} />}
      <fieldset className="weights"><legend><InfoLabel label="Value profile">Sets how strongly population, older adults, poverty, heat exposure, and passive built-environment burden influence site selection.</InfoLabel></legend><label className="field compact-field"><InfoLabel label="Apply a registered profile">Loads a saved combination of value weights; choose Custom weights to edit them manually.</InfoLabel><select aria-label="Apply a registered profile" defaultValue="" onChange={(event) => applyProfile(allProfiles.find((profile) => profile.name === event.target.value))}><option value="">Custom weights</option>{allProfiles.map((profile) => <option key={profile.name} value={profile.name}>{profile.label}</option>)}</select></label><div className="form-grid form-grid--weights">
        <WeightField name="population_density" label="Population" weights={weights} control={control} /><WeightField name="elderly_ratio" label="Older adults" weights={weights} control={control} /><WeightField name="poverty_rate" label="Poverty" weights={weights} control={control} /><WeightField name="heat_exposure" label="Heat exposure" weights={weights} control={control} /><WeightField name="environment_burden" label="Passive environment burden" weights={weights} control={control} />
      </div><div className={`weight-total ${Math.abs(weightTotal - 1) < .0001 ? "weight-total--ready" : "weight-total--warning"}`}><strong>Total: {weightTotal.toFixed(2)}</strong><span>{weightTotal < .9999 ? `Add ${(1 - weightTotal).toFixed(2)} before running.` : "Weights are ready."}</span></div>{errors.weights?.root && <small className="error">{errors.weights.root.message}</small>}</fieldset>
    </>}

    <fieldset className="weights"><legend><InfoLabel label="Operating window">Filters candidate facilities using the selected local date and service hours; you can choose whether facilities with unknown hours remain eligible.</InfoLabel></legend><label className="check-row"><input type="checkbox" {...register("operating_window_enabled")} /><span>Filter candidate availability by local service hours</span></label>{hasWindow && <div className="form-grid form-grid--four"><label className="field"><span>Date</span><input type="date" {...register("service_date")} /></label><label className="field"><span>Start</span><input type="time" {...register("service_start")} /></label><label className="field"><span>End</span><input type="time" {...register("service_end")} />{errors.service_end && <small className="error">{errors.service_end.message}</small>}</label><label className="field"><InfoLabel label="Unknown hours">Controls whether candidate facilities without published opening hours remain eligible for the run.</InfoLabel><select aria-label="Unknown hours" {...register("unknown_policy")}><option value="include">Include</option><option value="exclude">Exclude</option></select></label></div>}</fieldset>

    {sensitivity && <fieldset className="weights"><legend>Sensitivity grid</legend><div className="form-grid"><div className="field"><span>Thresholds (minutes)</span><Controller control={control} name="sensitivity_thresholds" render={({ field }) => <NumberListEditor value={field.value} onChange={field.onChange} />} />{errors.sensitivity_thresholds && <small className="error">{errors.sensitivity_thresholds.message}</small>}</div><div className="field"><span>Facility counts</span><Controller control={control} name="sensitivity_facility_counts" render={({ field }) => <NumberListEditor integer value={field.value} onChange={field.onChange} />} /></div></div><small>Maximum 12 values per list and 36 experiments total.</small></fieldset>}
      </div>
    </details>

    <div className="form-actions"><span>{ready ? "The confirmed task is ready to run." : "Confirm a study area before running."}</span><button type="submit" disabled={!ready || pending || (source === "csv" && !uploadedName)}>{pending ? "Starting…" : "Run confirmed agent"}</button></div>
    </div>
    </details>
  </form>;
}
