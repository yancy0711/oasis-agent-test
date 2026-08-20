import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { InfoLabel } from "./InfoLabel";

afterEach(cleanup);

describe("information disclosure", () => {
  it("toggles from its button and closes from an outside click", () => {
    render(<div><InfoLabel label="Threshold">Travel-time cutoff.</InfoLabel><button type="button">Outside</button></div>);
    const button = screen.getByRole("button", { name: "More information" });
    const tooltip = screen.getByRole("tooltip");

    fireEvent.click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(tooltip).toHaveClass("is-open");
    fireEvent.click(button);
    expect(button).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(button);
    fireEvent.pointerDown(screen.getByRole("button", { name: "Outside" }));
    expect(button).toHaveAttribute("aria-expanded", "false");
  });
});
