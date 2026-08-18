import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { RunProgress } from "./RunProgress";

describe("run progress", () => {
  it("keeps a solid full bar after a successful run", () => {
    render(<RunProgress status="succeeded" phase="precise" progress={null} />);
    expect(screen.getByText("Run complete")).toBeInTheDocument();
    expect(screen.getByText("100%")).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "100");
  });
});
