import { useEffect, useMemo, useState, type FormEvent } from "react";
import { listLlmModels, revokeLlmSession, testLlmConnection, type LlmConnectionInput } from "../../api/client";
import { CheckboxDropdown } from "../../components/CheckboxDropdown";
import { useAppStore } from "../../store/appStore";

type Provider = LlmConnectionInput["provider"];
type ConnectionState = "idle" | "success" | "failure";

const PRESETS: Record<Provider, { baseUrl: string; model: string }> = {
  openrouter: { baseUrl: "https://openrouter.ai/api/v1", model: "openai/gpt-4o" },
  openai: { baseUrl: "https://api.openai.com/v1", model: "gpt-4o" },
  custom: { baseUrl: "https://", model: "" },
  local: { baseUrl: "http://127.0.0.1:11434/v1", model: "qwen2.5" },
};

const EXAMPLE_MODELS: Partial<Record<Provider, string[]>> = {
  openai: ["gpt-4o", "gpt-4.1", "gpt-4.1-mini", "gpt-4o-mini"],
  local: ["qwen2.5:3b", "qwen2.5:7b", "llama3.2:3b", "llama3.1:8b", "gemma3:4b", "mistral:7b", "deepseek-r1:7b"],
};

const cleanModelId = (value: string) => value.trim().replace(/^~+/, "");

function modelGroup(model: string, provider: Provider, discovered: Set<string>) {
  if (provider === "openrouter") {
    const owner = model.split("/", 1)[0] || "other";
    const known: Record<string, string> = { openai: "OpenAI", anthropic: "Anthropic", google: "Google", qwen: "Qwen", deepseek: "DeepSeek", "meta-llama": "Meta Llama", mistralai: "Mistral", cohere: "Cohere" };
    return known[owner] ?? owner.replaceAll("-", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
  }
  if (discovered.has(model)) return provider === "local" ? "Installed models" : "Models available to this API key";
  return provider === "local" ? "Download examples" : "Common API examples";
}

export function LlmSettings({ defaults }: { defaults?: Record<string, unknown> }) {
  const initialProvider = (["openai", "openrouter", "custom", "local"].includes(String(defaults?.provider)) ? String(defaults?.provider) : "openrouter") as Provider;
  const [provider, setProvider] = useState(initialProvider);
  const [baseUrl, setBaseUrl] = useState(String(defaults?.base_url || PRESETS[initialProvider].baseUrl));
  const [model, setModel] = useState(PRESETS[initialProvider].model);
  const [apiKey, setApiKey] = useState("");
  const [discoveredModels, setDiscoveredModels] = useState<string[]>([]);
  const [pending, setPending] = useState(false);
  const [discovering, setDiscovering] = useState(false);
  const [message, setMessage] = useState("");
  const [connectionState, setConnectionState] = useState<ConnectionState>("idle");
  const [copiedCommand, setCopiedCommand] = useState("");
  const sessionLabel = useAppStore((state) => state.llmSessionLabel);
  const setLlmSession = useAppStore((state) => state.setLlmSession);
  const modelOptions = useMemo(() => {
    const discovered = new Set(discoveredModels);
    return [...new Set([...(model ? [model] : []), ...discoveredModels, ...(EXAMPLE_MODELS[provider] ?? [])].map(cleanModelId).filter(Boolean))]
      .map((value) => ({ value, label: value, group: modelGroup(value, provider, discovered) }))
      .sort((a, b) => a.group.localeCompare(b.group) || a.label.localeCompare(b.label));
  }, [discoveredModels, model, provider]);

  useEffect(() => {
    const clearOnBrowserExit = () => {
      const sessionId = useAppStore.getState().llmSessionId;
      if (sessionId) void revokeLlmSession(sessionId);
      setLlmSession(null, null);
    };
    window.addEventListener("pagehide", clearOnBrowserExit);
    return () => window.removeEventListener("pagehide", clearOnBrowserExit);
  }, [setLlmSession]);

  useEffect(() => {
    if (provider !== "openrouter") return;
    let active = true;
    setDiscovering(true);
    void listLlmModels({ provider: "openrouter", base_url: PRESETS.openrouter.baseUrl, api_key: "" })
      .then((models) => {
        if (!active) return;
        const available = models.map(cleanModelId).filter(Boolean);
        setDiscoveredModels(available);
        setModel((current) => available.includes(cleanModelId(current))
          ? cleanModelId(current)
          : available.includes(PRESETS.openrouter.model)
            ? PRESETS.openrouter.model
            : available[0] ?? "");
      })
      .catch((error) => {
        if (!active) return;
        setConnectionState("failure");
        setMessage(error instanceof Error ? `Failure: ${error.message}` : "Failure: model discovery failed.");
      })
      .finally(() => { if (active) setDiscovering(false); });
    return () => { active = false; };
  }, [provider]);

  const chooseProvider = (next: Provider) => {
    setProvider(next); setBaseUrl(PRESETS[next].baseUrl); setModel(PRESETS[next].model);
    setDiscoveredModels([]); setMessage(""); setConnectionState("idle");
  };

  async function discover(silent = false) {
    setDiscovering(true); if (!silent) setMessage(""); setConnectionState("idle");
    try {
      const models = await listLlmModels({ provider, base_url: baseUrl, api_key: apiKey });
      setDiscoveredModels(models.map(cleanModelId).filter(Boolean));
      if (!silent) setMessage(models.length ? `Found ${models.length} installed or available model${models.length === 1 ? "" : "s"}.` : "The service responded, but it reported no installed models.");
    } catch (error) {
      setConnectionState("failure");
      setMessage(error instanceof Error ? `Failure: ${error.message}` : "Failure: model discovery failed.");
    } finally { setDiscovering(false); }
  }

  const test = async (event: FormEvent) => {
    event.preventDefault(); setPending(true); setMessage(""); setConnectionState("idle");
    try {
      const previous = useAppStore.getState().llmSessionId;
      const result = await testLlmConnection({ provider, base_url: baseUrl, model, api_key: apiKey });
      if (previous && previous !== result.session_id) void revokeLlmSession(previous);
      setLlmSession(result.session_id, `${result.provider} | ${result.model}`); setApiKey("");
      setConnectionState("success");
      const resolved = result.resolved_model === result.model ? result.model : `${result.model} → ${result.resolved_model}`;
      setMessage(`Success! ${resolved} passed the OASIS task-structure check in ${result.latency_ms} ms. The key is cleared when this browser tab or the backend process closes.`);
    } catch (error) {
      setLlmSession(null, null); setConnectionState("failure");
      setMessage(error instanceof Error ? `Failure: ${error.message}` : "Failure: connection test failed.");
    } finally { setPending(false); }
  };

  const copy = async (command: string) => {
    try {
      await navigator.clipboard.writeText(command);
      setCopiedCommand(command); window.setTimeout(() => setCopiedCommand(""), 1800);
    } catch { setCopiedCommand(""); }
  };
  const baseUrlHelp = provider === "openai" ? "Official API root; no editing is needed." : provider === "openrouter" ? "OpenRouter API root; no editing is needed." : provider === "local" ? "For Ollama running on this computer, use http://127.0.0.1:11434/v1." : "Enter the provider's OpenAI-compatible API root, normally an HTTPS address ending in /v1.";

  return <details className="llm-settings" open>
    <summary><span className="llm-settings__step">Preliminary setup</span><strong>Connect a language model</strong><span className="llm-settings__session">{sessionLabel ?? "server default"}</span></summary>
    <form onSubmit={test}>
      <p className="llm-settings__intro">Please set this before describing the task when you want to use your own model. Otherwise the configured server default is used.</p>
      <div className="form-grid form-grid--four">
        <label className="field"><span>Provider</span><select aria-label="Provider" value={provider} onChange={(event) => chooseProvider(event.target.value as Provider)}><option value="openrouter">OpenRouter</option><option value="openai">OpenAI</option><option value="custom">Custom provider</option><option value="local">Local model</option></select></label>
        <div className="field"><span>Model</span><CheckboxDropdown ariaLabel="Model choices" single searchable portal shortcuts options={modelOptions} selected={model ? [cleanModelId(model)] : []} onChange={(values) => setModel(cleanModelId(values[0] ?? ""))} summary={cleanModelId(model) || "Select a model"} emptyMessage={discovering ? "Loading the model catalog..." : "No models match this filter."} footer={<label><span>Exact model ID</span><input aria-label="Exact model ID" value={model} onChange={(event) => setModel(cleanModelId(event.target.value))} placeholder="Type the exact model ID" /></label>} /></div>
        <label className="field"><span>Base URL</span><input aria-label="Base URL" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} disabled={provider === "openai" || provider === "openrouter"} /><small>{baseUrlHelp}</small></label>
        <label className="field"><span>{provider === "local" ? "API key (optional)" : "API key"}</span><input aria-label={provider === "local" ? "API key (optional)" : "API key"} type="password" autoComplete="off" value={apiKey} onChange={(event) => setApiKey(event.target.value)} /><small>The key is never saved in browser storage or run output.</small></label>
      </div>
      {provider === "local" && <section className="local-model-setup"><strong>Example setup (Windows PowerShell)</strong><p>Install Ollama, then download the model selected above. These are examples; OASIS does not download models.</p>{["winget install --id Ollama.Ollama -e", `ollama pull ${model || "qwen2.5:3b"}`].map((command) => <div className="copy-command" key={command}><code>{command}</code><button type="button" className="button--secondary" onClick={() => void copy(command)}>Copy{copiedCommand === command && <span className="copy-success" aria-label="Copied">{"\u2713"}</span>}</button></div>)}<p>Start Ollama, keep Base URL as <code>http://127.0.0.1:11434/v1</code>, leave API key empty, and check the installed model list.</p></section>}
      <div className="button-row"><button type="button" className="button--secondary" onClick={() => void discover(false)} disabled={discovering || (provider !== "local" && provider !== "openrouter" && !apiKey.trim())}>{discovering ? <>Checking models<span className="animated-ellipsis" aria-label="in progress" /></> : provider === "local" ? "Check installed models" : provider === "openrouter" ? "Refresh model catalog" : "Check available models"}</button><button type="submit" disabled={pending || !model.trim() || (provider !== "local" && !apiKey.trim())}>{pending ? <>Testing connection<span className="animated-ellipsis" aria-label="in progress" /></> : "Test and use connection"}</button></div>
      {message && <p className={`llm-connection-status llm-connection-status--${connectionState}`} aria-live="polite"><span aria-hidden="true">{connectionState === "success" ? "\u2713" : connectionState === "failure" ? "\u00d7" : "i"}</span>{message}</p>}
    </form>
  </details>;
}
