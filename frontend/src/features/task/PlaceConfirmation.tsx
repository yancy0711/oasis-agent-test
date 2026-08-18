import type { PlaceResolveResponse } from "../../api/models";

interface PlaceConfirmationProps {
  resolution: PlaceResolveResponse;
  pending: boolean;
  selectedCandidateId: string | null;
  onConfirm: (candidateId: string) => void;
}

export function PlaceConfirmation({ resolution, pending, selectedCandidateId, onConfirm }: PlaceConfirmationProps) {
  return (
    <section className="panel">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Step 2 - Study area</p>
          <h2>Confirm the boundary</h2>
        </div>
        {selectedCandidateId
          ? <span className="status status--success">Boundary selected</span>
          : resolution.ambiguous && <span className="status status--warning">Ambiguous query</span>}
      </div>
      <div className="candidate-list">
        {resolution.candidates.map((candidate) => (
          <button
            aria-pressed={selectedCandidateId === candidate.candidate_id}
            className={`candidate-card ${selectedCandidateId === candidate.candidate_id ? "is-selected" : ""}`}
            disabled={pending}
            key={candidate.candidate_id}
            onClick={() => onConfirm(candidate.candidate_id)}
            type="button"
          >
            <strong>{candidate.canonical_name}</strong>
            <span>{candidate.place_type} - {candidate.display_name}</span>
          </button>
        ))}
      </div>
    </section>
  );
}
