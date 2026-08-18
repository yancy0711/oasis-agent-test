/* The exported Markdown normalizer is intentionally unit-tested alongside this view. */
/* eslint-disable react-refresh/only-export-components */
import { useQuery } from "@tanstack/react-query";
import rehypeKatex from "rehype-katex";
import ReactMarkdown from "react-markdown";
import remarkMath from "remark-math";
import "katex/dist/katex.min.css";
import { runKeys } from "../../api/queryKeys";

interface ReportViewProps { runId: string; url?: string; narrative?: { decision_summary: string; self_reflection: string } | null; recoveryText?: string; }
const markdownPlugins = { remarkPlugins: [remarkMath], rehypePlugins: [rehypeKatex] };

export function normalizeReportMarkdown(source: string): string {
  return source
    .replace(/^# (?:Siting Decision|Candidate Facility Selection) Report\S+\s/m, "# Candidate Facility Selection Report — ")
    .replace(/^# Heat Exposure Review\S+\s/m, "# Heat Exposure Review — ")
    .replace(/Efficiency\S*Equity/g, "Efficiency–Equity")
    .replace(/efficienc\S*equity/g, "efficiency–equity")
    .replaceAll("`w_i`", "$w_i$")
    .replaceAll("`c_ij`", "$c_{ij}$")
    .replace(
      /```text\s*E_i = sum_g\(P_g LST_g\) \/ sum_g\(P_g\)\s*```/g,
      String.raw`$$
E_i = \frac{\sum_{g \in i}P_g\,\mathrm{LST}_g}{\sum_{g \in i}P_g}
$$`,
    )
    .replace(
      /```text\s*w_i = P_i \[beta_pop \+ beta_old z\(old_i\) \+ beta_pov z\(poverty_i\)\s*\+ beta_heat z\(heat_i\)\]\s*```/g,
      String.raw`$$
w_i=P_i\left[\beta_{\mathrm{pop}}+\beta_{\mathrm{old}}z(\mathrm{old}_i)+\beta_{\mathrm{pov}}z(\mathrm{poverty}_i)+\beta_{\mathrm{heat}}z(E_i)\right]
$$`,
    )
    .replace(/```text\s*w_i = P_i\s*```/g, String.raw`$$
w_i=P_i
$$`)
    .replace(
      /```text\s*c_ij = max\(0, 1 - d_ij \/ T\)\s*A_i\s*= sum_j\(c_ij q_ij\)\s*maximize\s+sum_i sum_j\(w_i c_ij q_ij\)\s*```/g,
      String.raw`$$
c_{ij}=\max\left(0,1-\frac{d_{ij}}{T}\right)
$$

$$
A_i=\sum_jc_{ij}q_{ij}
$$

$$
\max_{x,q}\quad\sum_i\sum_jw_ic_{ij}q_{ij}
$$`,
    )
    .replace(
      /```text\s*G = \[sum_i sum_k P_i P_k \|A_i - A_k\|\]\s*\/ \[2 sum_i\(P_i\) sum_i\(P_i A_i\)\]\s*mean access = sum_i\(P_i A_i\) \/ sum_i\(P_i\)\s*J_alpha = \(1 - alpha\) mean_access \+ alpha \(1 - G\)\s*```/g,
      String.raw`$$
G_P=\frac{\sum_i\sum_kP_iP_k\lvert A_i-A_k\rvert}{2(\sum_iP_i)(\sum_iP_iA_i)}
$$

$$
\bar A_P=\frac{\sum_iP_iA_i}{\sum_iP_i},\qquad
J_\alpha=(1-\alpha)\bar A_P+\alpha(1-G_P)
$$`,
    );
}

export function ReportView({ runId, url, narrative, recoveryText }: ReportViewProps) {
  const report = useQuery({ queryKey: runKeys.report(runId, "report"), queryFn: async () => { const response = await fetch(url!); if (!response.ok) throw new Error(`Report returned ${response.status}.`); return response.text(); }, enabled: Boolean(url) });
  if (!url) {
    if (narrative) return <article className="report-view"><h3>Decision summary</h3><ReactMarkdown {...markdownPlugins}>{narrative.decision_summary}</ReactMarkdown><h3>Self-reflection</h3><ReactMarkdown {...markdownPlugins}>{narrative.self_reflection}</ReactMarkdown></article>;
    if (recoveryText) return <article className="report-view"><h3>Feasibility diagnostic</h3><p>{recoveryText}</p></article>;
    return <p className="empty-tab">No report artifact is available.</p>;
  }
  if (report.isLoading) return <p className="empty-tab">Loading report…</p>;
  if (report.isError) return <p className="error error--block">{report.error.message}</p>;
  return <article className="report-view"><a className="download-link" href={url} download>Download Markdown</a><ReactMarkdown {...markdownPlugins}>{normalizeReportMarkdown(report.data ?? "")}</ReactMarkdown></article>;
}
