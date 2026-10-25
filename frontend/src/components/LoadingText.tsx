/** Reuse the shared ellipsis animation for pending text and keep idle text still. */
export function LoadingText({ children, active = true }: { children: string; active?: boolean }) {
  return <>{active ? children.replace(/(?:\.{3}|…)\s*$/, "") : children}
    {active && <span className="animated-ellipsis" aria-label="in progress" />}</>;
}
