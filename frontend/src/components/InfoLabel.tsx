import { useEffect, useId, useRef, useState } from "react";

export function InfoLabel({ label, children }: { label: string; children: string }) {
  const tooltipId = useId();
  const rootRef = useRef<HTMLSpanElement>(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [open]);
  return <span className="info-label">
    <span>{label}</span>
    <span ref={rootRef} className="info-disclosure">
      <button type="button" aria-label="More information" aria-describedby={tooltipId} aria-expanded={open}
        onPointerDown={(event) => event.stopPropagation()} onClick={(event) => {
          event.stopPropagation();
          setOpen((value) => !value);
        }}>i</button>
      <span id={tooltipId} className={`info-popover${open ? " is-open" : ""}`} role="tooltip">{children}</span>
    </span>
  </span>;
}
