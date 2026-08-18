import { useState, type MouseEvent as ReactMouseEvent } from "react";
import type { RunResult } from "../../api/models";

export type ResultRow = Record<string, unknown>;

function objectRecord(value: unknown): ResultRow {
  return value && typeof value === "object" && !Array.isArray(value) ? value as ResultRow : {};
}

function displayValue(value: unknown) {
  return value == null ? "—" : typeof value === "number"
    ? Number.isInteger(value) ? String(value) : value.toFixed(3)
    : typeof value === "object" ? JSON.stringify(value) : humanize(String(value));
}

function humanize(value: string) {
  const labels: Record<string, string> = {
    ors_sparse: "OpenRouteService network matrix",
    "live+cache": "Live and cached data",
    pca_loading: "PCA loading weights",
    user_custom: "User-defined weights",
    tract_mean_fallback: "Tract-average temperature fallback",
  };
  return labels[value] ?? value.replaceAll("_", " ");
}

function readableJson(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(readableJson);
  if (value && typeof value === "object") return Object.fromEntries(
    Object.entries(objectRecord(value)).map(([key, item]) => [humanize(key), readableJson(item)]),
  );
  return typeof value === "string" ? humanize(value) : value;
}

function ComplexValue({ value }: { value: unknown }) {
  if (typeof value === "string" && /^https?:\/\//i.test(value)) return <a href={value} target="_blank" rel="noreferrer">Open official link</a>;
  if (value == null || typeof value !== "object") return <>{displayValue(value)}</>;
  const count = Array.isArray(value) ? value.length : Object.keys(objectRecord(value)).length;
  const label = Array.isArray(value) ? `${count} items` : `${count} fields`;
  if (!count) return <span className="muted">None</span>;
  return <details className="cell-details"><summary>{label}</summary><pre>{JSON.stringify(readableJson(value), null, 2)}</pre></details>;
}

export function DataTable({ data, collapsedColumns = [] }: { data: ResultRow[]; collapsedColumns?: string[] }) {
  const [widths, setWidths] = useState<Record<string, number>>({});
  if (!data.length) return null;
  const columns = [...new Set(data.flatMap((item) => Object.keys(item)))];
  const defaultWidth = (column: string) => column.includes("selected") || column.includes("report") ? 220 : 140;
  const resize = (column: string, event: ReactMouseEvent) => {
    event.preventDefault();
    const start = event.clientX;
    const initial = widths[column] ?? defaultWidth(column);
    const move = (moveEvent: MouseEvent) => setWidths((current) => ({ ...current, [column]: Math.max(90, initial + moveEvent.clientX - start) }));
    const stop = () => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", stop);
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", stop);
  };
  return <div className="table-scroll"><table className="resizable-table"><thead><tr>{columns.map((column) => <th key={column} style={{ width: widths[column] ?? defaultWidth(column) }}>{column.replaceAll("_", " ")}<span className="column-resizer" role="separator" aria-label={`Resize ${column.replaceAll("_", " ")} column`} onMouseDown={(event) => resize(column, event)} /></th>)}</tr></thead><tbody>{data.map((item, index) => <tr key={index}>{columns.map((column) => <td key={column} style={{ width: widths[column] ?? defaultWidth(column) }}>{collapsedColumns.includes(column) && item[column] ? <details className="cell-details"><summary>Show note</summary><p>{displayValue(item[column])}</p></details> : <ComplexValue value={item[column]} />}</td>)}</tr>)}</tbody></table></div>;
}

export function ExecutionSummary({ result }: { result: RunResult }) {
  const execution = result.execution;
  const routing = objectRecord(result.routing_status);
  const routingRows = Object.entries(routing).map(([field, value]) => ({ field: field.replaceAll("_", " "), value }));
  return <div className={`execution-notice execution-notice--${execution.accuracy ?? "unknown"}`}><strong>{execution.accuracy === "network" ? "Precise network result" : execution.accuracy === "approximate" ? "Approximate travel-time result" : "Execution details"}</strong><span>{execution.phase} phase · requested {execution.router_requested} · used {execution.router_used ?? "unknown"}</span>{routingRows.length > 0 && <details><summary>Routing status</summary><DataTable data={routingRows} /></details>}</div>;
}
