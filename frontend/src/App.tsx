import { AppShell } from "./app/AppShell";
import { RunWorkspace } from "./features/run/RunWorkspace";
import { TaskWorkspace } from "./features/task/TaskWorkspace";
import { TaskEditorProvider } from "./features/task/TaskEditorContext";

export default function App() {
  return (
    <AppShell>
      <TaskEditorProvider>
        <TaskWorkspace />
        <RunWorkspace />
      </TaskEditorProvider>
    </AppShell>
  );
}
