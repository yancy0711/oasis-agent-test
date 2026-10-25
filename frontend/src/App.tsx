import { AppShell } from "./app/AppShell";
import { RunWorkspace } from "./features/run/RunWorkspace";
import { TaskWorkspace } from "./features/task/TaskWorkspace";
import { TaskEditorProvider } from "./features/task/TaskEditorContext";

import { useAppStore } from "./store/appStore";

export default function App() {
  const audience = useAppStore((state) => state.audience);
  return (
    <AppShell>
      <TaskEditorProvider>
        <TaskWorkspace />
        {audience === "research" && <RunWorkspace />}
      </TaskEditorProvider>
    </AppShell>
  );
}
