import { useWorkspacePreferences } from "../../app/WorkspacePreferences";
import { useRef, useState, type FormEvent, type ReactNode } from "react";
import { WorkspaceIcon } from "../../app/WorkspaceIcon";
import { LoadingText } from "../../components/LoadingText";

type PromptAudience = "research" | "personal";

const AUDIENCES: Record<PromptAudience, {
  label: string;
  description: string;
  placeholder: string;
  note?: string;
  examples: { label: string; prompt: string; action?: "current_location" }[];
}> = {
  research: {
    label: "Research and planning",
    description: "Analyze a study area through facility siting, value-profile comparison, heat exposure, or accessibility assessment.",
    placeholder: "Select 10 candidate cooling facilities in Phoenix within a 15-minute walk.",
    examples: [
      { label: "Candidate facility siting", prompt: "Select 10 candidate cooling facilities in Phoenix, Arizona within a 15-minute walking threshold." },
      { label: "Value profile comparison", prompt: "Compare efficiency and access inequality when selecting 10 candidate cooling facilities in Phoenix within a 15-minute walk." },
      { label: "Heat exposure assessment", prompt: "Do not optimize sites. Only show which Phoenix communities have the highest heat exposure." },
      { label: "Accessibility gap assessment", prompt: "Which Phoenix tracts are hardest to reach from the current candidate facilities within 15 minutes?" },
    ],
  },
  personal: {
    label: "Find a nearby facility",
    description: "Start from a specific US address or landmark and plan a route to a nearby candidate facility.",
    placeholder: "I'm at the Lincoln Memorial. How can I find the nearest cooling facility?",
    note: "Search nearby places without a facility budget. OASIS does not confirm real-time opening or emergency availability.",
    examples: [
      { label: "Use my location", prompt: "Use my current location to find the nearest cooling facility.", action: "current_location" },
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
  audience?: PromptAudience;
  onAudienceChange?: (audience: "research" | "personal") => void;
  onUseLocation?: () => void;
  autoDetect?: boolean;
  onAutoDetectChange?: (enabled: boolean) => void;
  onShowUpload?: () => void;
  hasActiveRun?: boolean;
  activity?: ReactNode;
}

const historyKey = "oasis-prompt-history";

function readHistory(): string[] {
  try {
    const entries: unknown = JSON.parse(sessionStorage.getItem(historyKey) ?? "[]");
    return Array.isArray(entries) ? entries.filter((value): value is string => typeof value === "string") : [];
  } catch {
    return [];
  }
}

export function PromptInput({ pending, onSubmit, onCancel, onUseLocation, audience: controlledAudience, onAudienceChange, autoDetect = true, onAutoDetectChange, onShowUpload, hasActiveRun, activity }: PromptInputProps) {
  const { t } = useWorkspacePreferences();
  const [prompt, setPrompt] = useState("");
  const [submitted, setSubmitted] = useState("");
  const [history, setHistory] = useState(readHistory);
  const [historyIndex, setHistoryIndex] = useState(-1);
  const originalDraft = useRef("");
  const [localAudience, setAudience] = useState<PromptAudience>("research");
  const audience = controlledAudience ?? localAudience;
  const composer = useRef<HTMLTextAreaElement>(null);
  const active = AUDIENCES[audience];
  const placeholder = t(active.placeholder);
  const canFillExample = prompt === "" && !pending;
  const showQuickStart = !submitted && !hasActiveRun && Boolean(onShowUpload);

  function fill(value: string) {
    setHistoryIndex(-1);
    setPrompt(value);
    composer.current?.focus();
  }

  function recall(index: number) {
    if (index >= history.length || index < -1) return;
    if (historyIndex < 0) originalDraft.current = prompt;
    setHistoryIndex(index);
    setPrompt(index < 0 ? originalDraft.current : history[index]);
    requestAnimationFrame(() => composer.current?.setSelectionRange(0, 0));
  }

  function remember(value: string) {
    setSubmitted(value);
    const next = [value, ...history];
    setHistory(next);
    setHistoryIndex(-1);
    try {
      sessionStorage.setItem(historyKey, JSON.stringify(next));
    } catch {
      // The current tab can still recall prompts when browser storage is unavailable.
    }
  }

  function analyze() {
    const value = prompt.trim();
    if (value && !pending) {
      remember(value);
      onSubmit(value);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    analyze();
  }

  return (
    <form className="prompt-input workbench-chat" onSubmit={submit}>
      <div className="workbench-chat__scroll">
      {showQuickStart && <div className="workbench-welcome">
        <h1>{t("Cooling-center siting workbench")}</h1>
        <p>{t("Ask about a US city or county, review a site plan, or bring your own candidate locations.")}</p>
      </div>}
      {submitted && <div className="workbench-user-message"><small>{t("You")}</small><p>{submitted}</p></div>}
      <div className="prompt-audience-tabs" role="tablist" aria-label={t("Choose how you want to use OASIS")}>
        {(Object.keys(AUDIENCES) as PromptAudience[]).map((key) => (
          <button
            type="button"
            role="tab"
            id={`prompt-tab-${key}`}
            aria-controls={`prompt-panel-${key}`}
            aria-selected={audience === key}
            className={audience === key ? "is-active" : ""}
            key={key}
            onClick={() => { setAudience(key); onAudienceChange?.(key); }}
          >
            {t(AUDIENCES[key].label)}
          </button>
        ))}
      </div>
      {onAutoDetectChange && <div className="workflow-detection"><label><input type="checkbox" checked={autoDetect} disabled={pending}
        onChange={(event) => onAutoDetectChange(event.target.checked)} />{t("Auto-detect task type")}</label>
        {!autoDetect && <small>{t("Manual choice: Analyze request uses this tab.")}</small>}</div>}
      <div className="prompt-audience-panel" role="tabpanel" id={`prompt-panel-${audience}`} aria-labelledby={`prompt-tab-${audience}`}>
        {showQuickStart && audience === "research" && <div className="workbench-quick-start">
          <h3>{t("Quick-start prompts")}</h3>
          <button type="button" onClick={() => fill(AUDIENCES.research.examples[0].prompt)}>{t("Site 10 cooling centers in Phoenix")} <WorkspaceIcon name="arrow" /></button>
          <button type="button" onClick={() => fill("Select cooling facilities in Phoenix, Arizona within a 15-minute walk. Prioritize residents aged 65 and older in South Phoenix.")}>{t("Prioritize residents aged 65+ in South Phoenix")} <WorkspaceIcon name="arrow" /></button>
          <button className="workbench-upload-button" type="button" onClick={onShowUpload}>{t("Use my own data")} <WorkspaceIcon name="upload" /></button>
          <button type="button" onClick={() => composer.current?.focus()}>{t("Write a prompt")} <WorkspaceIcon name="edit" /></button>
        </div>}
        <details key={audience} className="workbench-disclosure prompt-library" open={!onShowUpload || audience === "personal" ? true : undefined}>
        <summary>{t("More examples")}</summary>
        <p>{t(active.description)}</p>
        <div className="prompt-examples" aria-label={`${active.label} example requests`}>
          {active.examples.map((example) => <button type="button" key={example.label} onClick={() => {
            fill(example.prompt);
            if (example.action === "current_location") {
              remember(example.prompt);
              onUseLocation?.();
            }
          }}>{t(example.label)}</button>)}
        </div>
        </details>
        {active.note && <p className="prompt-audience-note">{t(active.note)}</p>}
      </div>
      {activity}
      </div>
      <div className="prompt-input__row">
        <textarea
          ref={composer}
          id="prompt"
          aria-label={t("Describe the cooling-access question")}
          aria-describedby={[canFillExample && "prompt-example-help", history.length > 0 && "prompt-history-help"].filter(Boolean).join(" ") || undefined}
          value={prompt}
          onChange={(event) => { setPrompt(event.target.value); setHistoryIndex(-1); }}
          rows={2}
          onKeyDown={(event) => {
            if (event.nativeEvent.isComposing) return;
            const input = event.currentTarget;
            const unmodified = !event.shiftKey && !event.altKey && !event.ctrlKey && !event.metaKey;
            if (event.key === "Tab" && unmodified && canFillExample) {
              event.preventDefault();
              fill(placeholder);
              return;
            }
            const atFirstLine = !input.value.slice(0, input.selectionStart).includes("\n");
            if (history.length && unmodified && input.selectionStart === input.selectionEnd) {
              if (event.key === "ArrowUp" && (historyIndex >= 0 || atFirstLine)) {
                event.preventDefault();
                recall(historyIndex + 1);
              }
              if (event.key === "ArrowDown" && historyIndex >= 0) {
                event.preventDefault();
                recall(historyIndex - 1);
              }
            }
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              analyze();
            }
          }}
          placeholder={placeholder}
        />
        {history.length > 0 && <span id="prompt-history-help" className="workbench-visually-hidden">{t("Press ↑ on the first line to recall earlier prompts. Press ↓ to return.")}</span>}
        {onShowUpload && audience === "research" && <button className="workbench-icon-button composer-upload" type="button" onClick={onShowUpload} aria-label={t("Upload candidate CSV")} title={t("Upload candidate CSV")}><WorkspaceIcon name="upload" /></button>}
        {canFillExample && <small id="prompt-example-help" className="prompt-example-help">{t("Tab to use example")}</small>}
        <button type="submit" disabled={pending || !prompt.trim()}>
          {pending ? <LoadingText>{t("Analyzing")}</LoadingText> : <>{t("Analyze request")} <WorkspaceIcon name="send" /></>}
        </button>
        {pending && <button type="button" className="button--danger" onClick={onCancel}>{t("Cancel")}</button>}
      </div>
    </form>
  );
}
