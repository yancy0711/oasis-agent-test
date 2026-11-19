import { useWorkspacePreferences } from "../../app/WorkspacePreferences";
import { useState } from "react";

interface CandidateUploadProps {
  pending: boolean;
  uploadedName: string | null;
  onUpload: (file: File) => void;
  onRemove?: () => void;
}

export function CandidateUpload({ pending, uploadedName, onUpload, onRemove }: CandidateUploadProps) {
  const { t } = useWorkspacePreferences();
  const [name, setName] = useState<string | null>(null);
  return <>
    <label className="field field--wide">
      <span>{t("Candidate CSV")}</span>
      <input
        type="file"
        accept=".csv,text/csv"
        disabled={pending}
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (!file) return;
          event.currentTarget.value = "";
          setName(file.name);
          onUpload(file);
        }}
      />
      {pending && name && <small>Uploading: {name}</small>}
    </label>
    {uploadedName && <div className="upload-status workbench-confirmed">
      <span role="status">Uploaded: {uploadedName}</span>
      {onRemove && <button type="button" className="upload-remove" title={t("Remove uploaded candidate CSV")}
        aria-label={t("Remove uploaded candidate CSV")} disabled={pending} onClick={onRemove}>×</button>}
    </div>}
  </>;
}
