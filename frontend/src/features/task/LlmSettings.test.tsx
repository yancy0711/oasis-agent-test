import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useAppStore } from "../../store/appStore";
import { LlmSettings } from "./LlmSettings";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  useAppStore.getState().resetSession();
});

describe("visitor model connection", () => {
  it("uses the configured Gemini model as the default while preserving provider switching", () => {
    render(<LlmSettings defaults={{
      provider: "gemini", model: "gemini-3.5-flash-lite",
      base_url: "https://generativelanguage.googleapis.com/v1beta/openai/",
    }} />);
    expect(screen.getByLabelText("Provider")).toHaveValue("gemini");
    expect(screen.getByRole("button", { name: "Model choices" })).toHaveTextContent("gemini-3.5-flash-lite");
    expect(screen.getByLabelText("Base URL")).toHaveValue("https://generativelanguage.googleapis.com/v1beta/openai/");
    expect(screen.getByText(/availability is determined by the OASIS backend location/)).toBeInTheDocument();
  });

  it("keeps preset Base URLs editable for compatible gateways", () => {
    render(<LlmSettings defaults={{ provider: "openrouter" }} />);
    const input = screen.getByLabelText("Base URL");
    expect(input).not.toBeDisabled();
    fireEvent.change(input, { target: { value: "https://gateway.example/v1" } });
    expect(input).toHaveValue("https://gateway.example/v1");
  });

  it("stores only the opaque tested session in UI state", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      session_id: "opaque-session", provider: "openai", model: "gpt-test", resolved_model: "gpt-test",
      latency_ms: 18, expires_in_seconds: 7200, structured_output: true,
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    render(<LlmSettings defaults={{ provider: "openai", model: "gpt-test", base_url: "https://api.openai.com/v1" }} />);
    fireEvent.click(screen.getByRole("button", { name: "Model choices" }));
    fireEvent.change(screen.getByLabelText("Exact model ID"), { target: { value: "gpt-test" } });
    fireEvent.change(screen.getByLabelText("API key"), { target: { value: "visitor-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Test connection" }));
    await waitFor(() => expect(screen.getByText(/gpt-test passed the OASIS task-structure check in 18 ms/)).toBeInTheDocument());
    expect(useAppStore.getState().llmSessionId).toBe("opaque-session");
    expect(screen.getByLabelText("API key")).toHaveValue("");
    expect(JSON.stringify(useAppStore.getState())).not.toContain("visitor-secret");
    expect(fetchMock).toHaveBeenCalledOnce();
    window.dispatchEvent(new Event("pagehide"));
    expect(useAppStore.getState().llmSessionId).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("explains that choosing a local model does not download one", () => {
    render(<LlmSettings defaults={{ provider: "openrouter" }} />);
    fireEvent.change(screen.getByLabelText("Provider"), { target: { value: "local" } });
    expect(screen.getByText(/OASIS does not download models/)).toBeInTheDocument();
    expect(screen.getByText(/ollama pull/)).toBeInTheDocument();
  });

  it("loads and groups the public OpenRouter catalog without requiring a key", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      provider: "openrouter",
      models: ["openai/gpt-4o", "~anthropic/claude-sonnet-4", "deepseek/deepseek-chat", "google/gemini-2.5-pro"],
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    render(<LlmSettings defaults={{ provider: "openrouter" }} />);
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole("button", { name: "Model choices" }));
    expect(screen.getAllByText("OpenAI")).toHaveLength(2);
    expect(screen.getByText("Anthropic")).toBeInTheDocument();
    expect(screen.getByText("Google")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "D" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "anthropic/claude-sonnet-4" }));
    expect(screen.getByRole("button", { name: "Model choices" })).toHaveTextContent("anthropic/claude-sonnet-4");
  });
});
