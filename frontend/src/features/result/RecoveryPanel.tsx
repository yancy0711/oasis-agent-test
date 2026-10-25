import { useState } from "react";
import type { RunResult } from "../../api/models";
import { useTaskEditor } from "../task/TaskEditorContext";
import { safeRecoveryPatch } from "./recovery.utils";
import { LoadingText } from "../../components/LoadingText";

interface Props { result: RunResult; onRetry?: () => void; retryPending?: boolean; retryError?: string; }
export function RecoveryPanel({ result, onRetry, retryPending = false, retryError }: Props) {
  const [dismissed, setDismissed] = useState(false);
  const editor = useTaskEditor();
  if (!result.recovery || dismissed) return null;
  const patch = safeRecoveryPatch(result.recovery.suggested_changes ?? {});
  const canApply = Object.keys(patch).length > 0;
  const osmUnavailable = /OpenStreetMap|Overpass/i.test(result.recovery.diagnosis)
    && /failed|unavailable|timeout|timed out|budget|cooling down|rate limit/i.test(result.recovery.diagnosis);
  const timedOut = /ReadTimeout|timed out|time.?out/i.test(result.recovery.diagnosis);
  const solverTimedOut = !osmUnavailable
    && /solve|optimization/i.test(result.recovery.diagnosis)
    && /time limit|timed out|timeout/i.test(result.recovery.diagnosis);
  const reviewSettings = (csv = false) => {
    if (csv) editor.apply({ candidate_source: "csv" });
    const settings = document.querySelector<HTMLDetailsElement>(".task-settings-disclosure");
    if (settings) settings.open = true;
    const form = document.querySelector(".task-workspace form.panel");
    form?.scrollIntoView({ behavior: "smooth", block: "start" });
    form?.querySelector<HTMLSelectElement>('[aria-label="Candidate source"]')?.focus();
  };
  const needsStudyArea = /study.?area|city or county|boundary type/i.test(
    `${result.recovery.diagnosis} ${result.recovery.question}`,
  );
  const reviewStudyArea = () => {
    setDismissed(true);
    const section = document.querySelector("[data-study-area-step]");
    if (section instanceof HTMLDetailsElement) section.open = true;
    const target = section?.querySelector(".candidate-list, input") ?? document.querySelector("#prompt");
    target?.scrollIntoView({ behavior: "smooth", block: "center" });
    if (target instanceof HTMLElement) target.focus();
  };
  return <div className={`recovery-panel${result.execution?.status === "failed" ? " error" : ""}`} role="alert">
    <strong>Human decision required</strong>
    {osmUnavailable ? <>
      <p>{timedOut ? "The OSM lookup timed out before a complete response arrived." : "The configured OpenStreetMap services could not complete this lookup."} Your confirmed task is kept.</p>
      <p>Retry unchanged, or review fewer facility types or a smaller study area. Those edits change the search scope. For repeated failures, switch services below or use a reviewed candidate CSV.</p>
      <details><summary>Service options</summary>
        <p>An administrator can switch to an independent hosted or self-hosted Overpass service. Set <code>OASIS_OVERPASS_URL</code> in the backend <code>.env</code>, restart the backend, then retry. This changes the service, not the OSM candidate source.</p>
        <p>A hosted service such as <a href="https://overpass.ch/docs" target="_blank" rel="noreferrer">overpass.ch</a> requires an account/key. Set <code>OASIS_OVERPASS_URL=https://api.overpass.ch/v1/interpreter</code> and <code>OVERPASS_CH_API_KEY</code> in the backend environment. A resident-only <code>RESIDENT_OVERPASS_URL</code> takes precedence if set. Test the actual demo query before presenting.</p>
      </details>
      <details><summary>Provider diagnostics</summary><p>{result.recovery.diagnosis}</p></details>
    </> : <><p>{result.recovery.diagnosis}</p><p>{result.recovery.question}</p></>}
    {canApply && <dl className="change-list">{Object.entries(patch).filter(([key]) => key !== "coverage_target_enabled").map(([key, value]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{String(value)}</dd></div>)}</dl>}
    <div className="button-row">
      {solverTimedOut && onRetry && <button type="button" disabled={retryPending} onClick={onRetry}>{retryPending ? <LoadingText>Starting retry</LoadingText> : "Retry same task"}</button>}
      {osmUnavailable && <>
        {onRetry && <button type="button" disabled={retryPending} onClick={onRetry}>{retryPending ? <LoadingText>Starting retry</LoadingText> : "Retry same task"}</button>}
        <button type="button" className="button--secondary" disabled={retryPending} onClick={() => reviewSettings()}>Edit search settings</button>
        <button type="button" className="button--secondary" disabled={retryPending} onClick={() => reviewSettings(true)}>Use candidate CSV</button>
      </>}
      {canApply && <button type="button" onClick={() => { editor.apply(patch); setDismissed(true); document.querySelector(".task-workspace form.panel")?.scrollIntoView({ behavior: "smooth" }); }}>Apply to task editor</button>}
      {needsStudyArea && <button type="button" onClick={reviewStudyArea}>Choose a city or county</button>}
      {!osmUnavailable && <button type="button" className="button--secondary" onClick={() => setDismissed(true)}>Keep editing manually</button>}
    </div>
    {retryError && <p className="error">{retryError}</p>}
  </div>;
}
