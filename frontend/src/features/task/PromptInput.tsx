import { useState, type FormEvent } from "react";
import { InfoLabel } from "../../components/InfoLabel";

type PromptAudience = "research" | "personal";

const AUDIENCES: Record<PromptAudience, {
  label: string;
  audience: string;
  description: string;
  placeholder: string;
  note?: string;
  examples: { label: string; prompt: string }[];
}> = {
  research: {
    label: "Research and planning",
    audience: "For planners, public-health teams, and researchers.",
    description: "Analyze a US city or county using candidate selection, equity, heat, or reachability workflows.",
    placeholder: "Select 10 candidate cooling facilities in Phoenix within a 15-minute walk.",
    examples: [
      { label: "Candidate selection", prompt: "Select 10 candidate cooling facilities in Phoenix, Arizona within a 15-minute walking threshold." },
      { label: "Equity trade-off", prompt: "Compare efficiency and access inequality when selecting 10 candidate cooling facilities in Phoenix within a 15-minute walk." },
      { label: "Heat review", prompt: "Do not optimize sites. Only show which Phoenix communities have the highest heat exposure." },
      { label: "Reachability review", prompt: "Which Phoenix tracts are hardest to reach from the current candidate facilities within 15 minutes?" },
      { label: "Needs clarification", prompt: "Help me improve cooling access." },
      { label: "Outside scope", prompt: "Which Phoenix cooling centers are open right now?" },
    ],
  },
  personal: {
    label: "Find a nearby facility",
    audience: "For Users starting from a specific address or landmark.",
    description: "Start from a specific US address or landmark and plan a route to a nearby candidate facility.",
    placeholder: "I'm at the Lincoln Memorial. How can I find the nearest cooling facility?",
    note: "OASIS can plan to candidate facilities after analysis, but it does not confirm real-time opening or emergency availability.",
    examples: [
      { label: "Landmark route", prompt: "I'm at the Lincoln Memorial. How can I find the nearest cooling facility?" },
      { label: "Address route", prompt: "I'm at 1681 Broadway, New York. How can I find the nearest cooling facility?" },
      { label: "Ambiguous place", prompt: "I'm at Disney. How can I find the nearest cooling facility?" },
    ],
  },
};

interface PromptInputProps {
  pending: boolean;
  onSubmit: (prompt: string) => void;
  onCancel: () => void;
}

export function PromptInput({ pending, onSubmit, onCancel }: PromptInputProps) {
  const [prompt, setPrompt] = useState("");
  const [audience, setAudience] = useState<PromptAudience>("research");
  const active = AUDIENCES[audience];

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
      <div className="prompt-audience-tabs" role="tablist" aria-label="Choose how you want to use OASIS">
        {(Object.keys(AUDIENCES) as PromptAudience[]).map((key) => (
          <button
            type="button"
            role="tab"
            id={`prompt-tab-${key}`}
            aria-controls={`prompt-panel-${key}`}
            aria-selected={audience === key}
            className={audience === key ? "is-active" : ""}
            key={key}
            onClick={() => setAudience(key)}
          >
            {AUDIENCES[key].label}
          </button>
        ))}
      </div>
      <div className="prompt-audience-panel" role="tabpanel" id={`prompt-panel-${audience}`} aria-labelledby={`prompt-tab-${audience}`}>
        <strong className="prompt-audience-for">{active.audience}</strong>
        <p>{active.description}</p>
        <div className="prompt-examples" aria-label={`${active.label} example requests`}>
          {active.examples.map((example) => <button type="button" key={example.label} onClick={() => setPrompt(example.prompt)}>{example.label}</button>)}
        </div>
        {active.note && <p className="prompt-audience-note">{active.note}</p>}
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
          placeholder={active.placeholder}
        />
        <button type="submit" disabled={pending || !prompt.trim()}>
          {pending ? "Analyzing..." : "Analyze request"}
        </button>
        {pending && <button type="button" className="button--danger" onClick={onCancel}>Cancel</button>}
      </div>
    </form>
  );
}
