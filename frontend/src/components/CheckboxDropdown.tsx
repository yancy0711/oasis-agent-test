import { useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { createPortal } from "react-dom";

export interface CheckboxDropdownOption { value: string; label: string; group?: string; }

interface CheckboxDropdownProps {
  ariaLabel: string; options: readonly CheckboxDropdownOption[]; selected: string[];
  summary: string; detail?: string; single?: boolean; searchable?: boolean; actions?: ReactNode; footer?: ReactNode;
  emptyMessage?: ReactNode; portal?: boolean; shortcuts?: boolean;
  defaultOpen?: boolean;
  onChange: (selected: string[]) => void;
}

/** Compact Excel-style checkbox filter used by bounded option selectors. */
export function CheckboxDropdown({ ariaLabel, options, selected, summary, detail, single = false, searchable = false, actions, footer, emptyMessage = "No options match this filter.", portal = false, shortcuts = false, defaultOpen = false, onChange }: CheckboxDropdownProps) {
  const [open, setOpen] = useState(defaultOpen);
  const [query, setQuery] = useState("");
  const [menuStyle, setMenuStyle] = useState<CSSProperties>();
  const rootRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) { setQuery(""); return; }
    const close = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!rootRef.current?.contains(target) && !menuRef.current?.contains(target)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    document.addEventListener("pointerdown", close); document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", close); document.removeEventListener("keydown", escape); };
  }, [open]);
  useEffect(() => {
    if (!open || !portal) return;
    const position = () => {
      const rect = rootRef.current?.getBoundingClientRect();
      if (!rect) return;
      const width = Math.min(720, window.innerWidth - 24);
      const left = Math.max(12, Math.min(rect.left, window.innerWidth - width - 12));
      setMenuStyle({ position: "fixed", top: rect.bottom + 5, left, width, maxHeight: Math.max(240, window.innerHeight - rect.bottom - 17) });
    };
    position();
    window.addEventListener("resize", position);
    window.addEventListener("scroll", position, true);
    return () => { window.removeEventListener("resize", position); window.removeEventListener("scroll", position, true); };
  }, [open, portal]);
  const toggle = (value: string, checked: boolean) => {
    if (single) { if (checked) { onChange([value]); setOpen(false); } return; }
    onChange(checked ? [...new Set([...selected, value])] : selected.filter((item) => item !== value));
  };
  const visibleOptions = query.trim()
    ? options.filter((option) => `${option.label} ${option.value}`.toLowerCase().includes(query.trim().toLowerCase()))
    : options;
  const shortcutLetters = useMemo(() => [...new Set(
    options.map((option) => option.group?.trim().charAt(0).toUpperCase())
      .filter((letter): letter is string => Boolean(letter)),
  )].sort(), [options]);
  const jumpTo = (letter: string) => menuRef.current?.querySelector<HTMLElement>(`[data-group-initial="${letter}"]`)?.scrollIntoView({ block: "start" });
  const menu = <div ref={menuRef} className={`checkbox-dropdown__menu${portal ? " checkbox-dropdown__menu--portal" : ""}`} style={portal ? menuStyle : undefined} role="listbox" aria-multiselectable={!single}>
    {actions && <div className="checkbox-dropdown__actions">{actions}</div>}
    {searchable && <div className="checkbox-dropdown__search"><input aria-label={`Filter ${ariaLabel}`} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter options" /></div>}
    <div className="checkbox-dropdown__body">
      <div className="checkbox-dropdown__options">{visibleOptions.map((option, index) => <div key={option.value}>{option.group && option.group !== visibleOptions[index - 1]?.group && <p className="checkbox-dropdown__group" data-group-initial={option.group.trim().charAt(0).toUpperCase()}>{option.group}</p>}<label className="checkbox-dropdown__option"><input type="checkbox" checked={selected.includes(option.value)} onChange={(event) => toggle(option.value, event.target.checked)} /><span>{option.label}</span></label></div>)}{!visibleOptions.length && <p className="checkbox-dropdown__empty">{emptyMessage}</p>}</div>
      {shortcuts && shortcutLetters.length > 1 && <nav className="checkbox-dropdown__shortcuts" aria-label="Jump to model provider">{shortcutLetters.map((letter) => <button type="button" key={letter} onClick={() => jumpTo(letter)}>{letter}</button>)}</nav>}
    </div>
    {footer && <div className="checkbox-dropdown__footer">{footer}</div>}
  </div>;
  return <div ref={rootRef} className={`checkbox-dropdown${open ? " is-open" : ""}`}>
    <button type="button" className="checkbox-dropdown__trigger" aria-label={ariaLabel} aria-haspopup="listbox" aria-expanded={open} onClick={() => setOpen((value) => !value)}><span><strong>{summary}</strong>{detail && <small>{detail}</small>}</span><i aria-hidden="true" /></button>
    {open && (portal ? createPortal(menu, document.body) : menu)}
  </div>;
}
