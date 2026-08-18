import { useState, type FormEvent } from "react";
import { InfoLabel } from "../../components/InfoLabel";

const EXAMPLES = [
  { label: "Candidate selection", prompt: "Select 10 candidate cooling facilities in Phoenix, Arizona within a 15-minute walking threshold." },
  { label: "Equity trade-off", prompt: "Compare efficiency and access inequality when selecting 10 candidate cooling facilities in Phoenix within a 15-minute walk." },
  { label: "Heat review", prompt: "Do not optimize sites. Only show which Phoenix communities have the highest heat exposure." },
  { label: "Reachability review", prompt: "Which Phoenix tracts are hardest to reach from the current candidate facilities within 15 minutes?" },
  { label: "Point route", prompt: "I'm at the Lincoln Memorial. How can I find the nearest cooling facility?" },
  { label: "Needs details", prompt: "Help me improve cooling access." },
  { label: "Outside scope", prompt: "Which Phoenix cooling centers are open right now?" },
];

interface PromptInputProps {
  pending: boolean;
  onSubmit: (prompt: string) => void;
  onCancel: () => void;
}

export function PromptInput({ pending, onSubmit, onCancel }: PromptInputProps) {
  const [prompt, setPrompt] = useState("");

  function analyze() {
    const value = prompt.trim();
    if (value && !pending) onSubmit(value);
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    analyze();
  }

  return (
    <form className="prompt-input" onSubmit={submit}>
      <div className="prompt-label">
        <label htmlFor="prompt"><span className="form-step">Step 1</span> Describe the cooling-access question</label>
        <InfoLabel label="Describe the cooling-access question">State the US city or county, candidate-facility question, travel threshold, and policy priorities. Unsupported real-time or parcel questions return a suggested official alternative.</InfoLabel>
      </div>
      <div className="prompt-examples" aria-label="Example requests">
        {EXAMPLES.map((example) => <button type="button" key={example.label} onClick={() => setPrompt(example.prompt)}>{example.label}</button>)}
      </div>
      <div className="prompt-input__row">
        <input
          id="prompt"
          aria-label="Describe the cooling-access question"
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              analyze();
            }
          }}
          placeholder="Select 10 candidate cooling facilities in Phoenix within a 15-minute walk."
        />
        <button type="submit" disabled={pending || !prompt.trim()}>
          {pending ? "Analyzing..." : "Analyze request"}
        </button>
        {pending && <button type="button" className="button--danger" onClick={onCancel}>Cancel</button>}
      </div>
    </form>
  );
}
