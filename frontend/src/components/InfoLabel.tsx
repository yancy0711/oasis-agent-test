import { useId } from "react";

export function InfoLabel({ label, children }: { label: string; children: string }) {
  const tooltipId = useId();
  return <span className="info-label">
    <span>{label}</span>
    <span className="info-disclosure">
      <button type="button" aria-label="More information" aria-describedby={tooltipId}>i</button>
      <span id={tooltipId} className="info-popover" role="tooltip">{children}</span>
    </span>
  </span>;
}
