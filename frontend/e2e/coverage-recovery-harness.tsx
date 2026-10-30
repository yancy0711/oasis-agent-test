/// <reference lib="dom" />
import { useState } from "react";
import { createRoot } from "react-dom/client";
import type { RunResult } from "../src/api/models";
import { RecoveryPanel } from "../src/features/result/RecoveryPanel";
import { TaskForm } from "../src/features/task/TaskForm";
import { TaskEditorProvider } from "../src/features/task/TaskEditorContext";
import { taskFromDraft, toTaskSpec } from "../src/features/task/taskForm.mapper";
import "../src/styles/globals.css";

const task = taskFromDraft({ place_name: "Riverside", coverage_target: .99, n_facilities: 10, threshold_minutes: 12 });
const result = { task, recovery: {
  diagnosis: "The confirmed plan is infeasible.", question: "Coverage target stays 99%. Recommended: smaller relative increase.",
  suggested_changes: { n_facilities: 15 }, alternatives: [
    { parameter: "n_facilities", status: "verified", description: "Keep 12 minutes: use 15 facilities.", changes: { n_facilities: 15 } },
    { parameter: "threshold_minutes", status: "verified", description: "Keep 10 facilities: use 102.7 minutes.", changes: { threshold_minutes: 102.7 } },
  ],
} } as RunResult;

// eslint-disable-next-line react-refresh/only-export-components
function Harness() {
  const [submitted, setSubmitted] = useState("");
  return <TaskEditorProvider><TaskForm task={task} mode="quick" ready pending={false} uploadPending={false} uploadedName={null}
    profiles={[]} onModeChange={() => {}} onUpload={() => {}} onSubmit={values => setSubmitted(JSON.stringify(toTaskSpec(task, values, task.place_name)))} />
    <RecoveryPanel result={result} /><output data-testid="submitted-task">{submitted}</output></TaskEditorProvider>;
}
createRoot(document.getElementById("root")!).render(<Harness />);
