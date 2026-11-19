import { TaskWorkspace } from "./features/task/TaskWorkspace";
import { TaskEditorProvider } from "./features/task/TaskEditorContext";
import { WorkspacePreferences } from "./app/WorkspacePreferences";

export default function App() {
  return (
    <WorkspacePreferences>
      <TaskEditorProvider>
        <TaskWorkspace />
      </TaskEditorProvider>
    </WorkspacePreferences>
  );
}
