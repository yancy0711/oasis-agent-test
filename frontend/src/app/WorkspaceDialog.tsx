import { useEffect, useId, useRef, type ReactNode } from "react";
import { WorkspaceIcon } from "./WorkspaceIcon";
import { useWorkspacePreferences } from "./WorkspacePreferences";

export function WorkspaceDialog({ title, open, onClose, wide, children }: {
  title: string;
  open: boolean;
  onClose: () => void;
  wide?: boolean;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const label = useId();
  const { t, language } = useWorkspacePreferences();

  useEffect(() => {
    const dialog = ref.current;
    if (open && !dialog?.open) dialog?.showModal();
    if (!open && dialog?.open) dialog.close();
  }, [open]);

  function dismiss() {
    ref.current?.close();
    onClose();
  }

  return <dialog ref={ref} className={`workbench-dialog${wide ? " workbench-dialog--wide" : ""}`} aria-labelledby={label}
    onCancel={(event) => { event.preventDefault(); dismiss(); }}>
    <header>
      <h2 id={label}>{t(title)}</h2>
      <button className="workbench-icon-button" type="button" onClick={dismiss} aria-label={language === "en" ? `Close ${title.toLowerCase()}` : t("Close dialog")}><WorkspaceIcon name="close" /></button>
    </header>
    <div className="workbench-dialog__body">{children}</div>
  </dialog>;
}
