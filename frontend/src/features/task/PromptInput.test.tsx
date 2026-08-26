import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PromptInput } from "./PromptInput";

afterEach(cleanup);

describe("prompt examples", () => {
  it("copies an example into the editable prompt without submitting it", () => {
    const submit = vi.fn();
    render(<PromptInput pending={false} onSubmit={submit} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Candidate facility siting" }));
    expect(screen.getByLabelText("Describe the cooling-access question")).toHaveValue(
      "Select 10 candidate cooling facilities in Phoenix, Arizona within a 15-minute walking threshold.",
    );
    expect(submit).not.toHaveBeenCalled();
  });

  it("groups research and personal route examples without clearing typed text", () => {
    render(<PromptInput pending={false} onSubmit={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Candidate facility siting" })).toBeInTheDocument();
    expect(screen.getAllByRole("button").filter((button) => button.closest(".prompt-examples"))).toHaveLength(4);
    expect(screen.queryByRole("button", { name: "Needs clarification" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Outside scope" })).not.toBeInTheDocument();
    const input = screen.getByLabelText("Describe the cooling-access question");
    fireEvent.change(input, { target: { value: "My unfinished question" } });

    fireEvent.click(screen.getByRole("tab", { name: "Find a nearby facility" }));

    expect(input).toHaveValue("My unfinished question");
    expect(screen.queryByRole("button", { name: "Candidate facility siting" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Landmark route" })).toBeInTheDocument();
    expect(screen.getByText(/does not confirm real-time opening/i)).toBeInTheDocument();
  });

  it("copies a personal route example into the same prompt field", () => {
    render(<PromptInput pending={false} onSubmit={vi.fn()} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByRole("tab", { name: "Find a nearby facility" }));
    fireEvent.click(screen.getByRole("button", { name: "Address route" }));
    expect(screen.getByLabelText("Describe the cooling-access question")).toHaveValue(
      "I'm at 1681 Broadway, New York. How can I find the nearest cooling facility?",
    );
  });

  it("offers current location before landmark routing and invokes geolocation", () => {
    const locate = vi.fn();
    render(<PromptInput pending={false} onSubmit={vi.fn()} onCancel={vi.fn()} onUseLocation={locate} />);
    fireEvent.click(screen.getByRole("tab", { name: "Find a nearby facility" }));
    const current = screen.getByRole("button", { name: "Use my location" });
    const landmark = screen.getByRole("button", { name: "Landmark route" });
    expect(current.compareDocumentPosition(landmark) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.click(current);
    expect(locate).toHaveBeenCalledOnce();
    expect(screen.getByLabelText("Describe the cooling-access question")).toHaveValue(
      "Use my current location to find the nearest cooling facility.",
    );
  });

  it("submits the current prompt when Enter is pressed", () => {
    const submit = vi.fn();
    render(<PromptInput pending={false} onSubmit={submit} onCancel={vi.fn()} />);
    const input = screen.getByLabelText("Describe the cooling-access question");
    fireEvent.change(input, { target: { value: "Compare cooling access in Phoenix" } });
    fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
    expect(submit).toHaveBeenCalledWith("Compare cooling access in Phoenix");
  });

  it("offers a direct cancellation control while request analysis is pending", () => {
    const cancel = vi.fn();
    render(<PromptInput pending onSubmit={vi.fn()} onCancel={cancel} />);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(cancel).toHaveBeenCalledOnce();
  });
});
