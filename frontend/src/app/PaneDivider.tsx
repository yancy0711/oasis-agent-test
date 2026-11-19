import { useRef, type RefObject } from "react";
import { useWorkspacePreferences } from "./WorkspacePreferences";

export function PaneDivider({ side, pane, min, max, value, onResize }: {
  side: "left" | "right";
  pane: RefObject<HTMLElement | null>;
  min: number;
  max: number;
  value: number;
  onResize: (width: number) => void;
}) {
  const { t } = useWorkspacePreferences();
  const drag = useRef<{ x: number; width: number } | null>(null);
  const sign = side === "left" ? 1 : -1;
  const label = t(side === "left" ? "Resize workspace panel" : "Resize assistant panel");
  const resize = (width: number) => onResize(Math.round(Math.max(min, Math.min(max, width))));
  return <div className={`pane-divider pane-divider--${side}`} role="separator" tabIndex={0}
    title={label} aria-label={label} aria-orientation="vertical"
    aria-controls={side === "left" ? "workspace-inspector" : "workspace-assistant"}
    aria-valuemin={min} aria-valuemax={max} aria-valuenow={value}
    onPointerDown={(event) => {
      if (event.button !== 0 || !pane.current) return;
      drag.current = { x: event.clientX, width: pane.current.clientWidth };
      event.currentTarget.setPointerCapture(event.pointerId);
      event.currentTarget.focus();
      event.preventDefault();
    }}
    onPointerMove={(event) => {
      if (drag.current && event.currentTarget.hasPointerCapture(event.pointerId)) resize(drag.current.width + sign * (event.clientX - drag.current.x));
    }}
    onLostPointerCapture={() => { drag.current = null; }}
    onKeyDown={(event) => {
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        resize(value + sign * (event.key === "ArrowLeft" ? -16 : 16));
      }
    }}
  />;
}
