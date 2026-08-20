import { useState } from "react";
import type { RunResult } from "../../api/models";
import { useTaskEditor } from "../task/TaskEditorContext";
import { safeRecoveryPatch } from "./recovery.utils";

export function RecoveryPanel({ result }: { result: RunResult }) {
  const [dismissed, setDismissed] = useState(false);
  const editor = useTaskEditor();
  if (!result.recovery || dismissed) return null;
  const patch = safeRecoveryPatch(result.recovery.suggested_changes ?? {});
  const canApply = Object.keys(patch).length > 0;
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
  return <div className="recovery-panel">
    <strong>Human decision required</strong>
    <p>{result.recovery.diagnosis}</p><p>{result.recovery.question}</p>
    {canApply && <dl className="change-list">{Object.entries(patch).filter(([key]) => key !== "coverage_target_enabled").map(([key, value]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{String(value)}</dd></div>)}</dl>}
    <div className="button-row">
      {canApply && <button type="button" onClick={() => { editor.apply(patch); setDismissed(true); document.querySelector(".task-workspace form.panel")?.scrollIntoView({ behavior: "smooth" }); }}>Apply to task editor</button>}
      {needsStudyArea && <button type="button" onClick={reviewStudyArea}>Choose a city or county</button>}
      <button type="button" className="button--secondary" onClick={() => setDismissed(true)}>Keep editing manually</button>
    </div>
  </div>;
}
