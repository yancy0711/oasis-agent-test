import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PromptInput } from "./PromptInput";

afterEach(cleanup);

describe("prompt examples", () => {
  it("copies an example into the editable prompt without submitting it", () => {
    const submit = vi.fn();
    render(<PromptInput pending={false} onSubmit={submit} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Candidate selection" }));
    expect(screen.getByLabelText("Describe the cooling-access question")).toHaveValue(
      "Select 10 candidate cooling facilities in Phoenix, Arizona within a 15-minute walking threshold.",
    );
    expect(submit).not.toHaveBeenCalled();
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
