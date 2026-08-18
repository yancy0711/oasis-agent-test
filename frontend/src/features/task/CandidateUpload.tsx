import { useState } from "react";

interface CandidateUploadProps {
  pending: boolean;
  uploadedName: string | null;
  onUpload: (file: File) => void;
}

export function CandidateUpload({ pending, uploadedName, onUpload }: CandidateUploadProps) {
  const [name, setName] = useState<string | null>(uploadedName);
  return (
    <label className="field field--wide">
      <span>Candidate CSV</span>
      <input
        type="file"
        accept=".csv,text/csv"
        disabled={pending}
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (!file) return;
          setName(file.name);
          onUpload(file);
        }}
      />
      {name && <small>{pending ? "Uploading" : "Selected"}: {name}</small>}
    </label>
  );
}
