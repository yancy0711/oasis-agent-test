import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { normalizeReportMarkdown, ReportView } from "./ReportView";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("report math", () => {
  it("upgrades legacy equation code blocks to Markdown math", () => {
    const source = `\`\`\`text
c_ij = max(0, 1 - d_ij / T)
A_i  = sum_j(c_ij q_ij)
maximize  sum_i sum_j(w_i c_ij q_ij)
\`\`\``;
    const upgraded = normalizeReportMarkdown(source);
    expect(upgraded).toContain(String.raw`c_{ij}=\max`);
    expect(upgraded).not.toContain("```text");
  });

  it("does not duplicate the separator in a newly generated report title", () => {
    expect(normalizeReportMarkdown("# Siting Decision Report — Phoenix"))
      .toBe("# Siting Decision Report — Phoenix");
  });

  it("upgrades legacy model variable code spans to inline math", () => {
    expect(normalizeReportMarkdown("not included in `w_i` or `c_ij`"))
      .toBe("not included in $w_i$ or $c_{ij}$");
  });

  it("typesets report equations with KaTeX", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      text: async () => String.raw`## Formula

$$
E_i=\frac{\sum_g P_g\mathrm{LST}_g}{\sum_gP_g}
$$`,
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { container } = render(
      <QueryClientProvider client={client}>
        <ReportView runId="run-1" url="/report.md" />
      </QueryClientProvider>,
    );
    expect(await screen.findByRole("heading", { name: "Formula" })).toBeInTheDocument();
    await waitFor(() => expect(container.querySelector(".katex-display")).toBeInTheDocument());
  });
});
