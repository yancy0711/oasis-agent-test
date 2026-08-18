import type { PropsWithChildren } from "react";

export function AppShell({ children }: PropsWithChildren) {
  return (
    <div className="app-shell">
      <header className="site-header">
        <a className="brand" href="/" aria-label="OASIS home">
          <span className="brand-mark">O</span>
          <span><strong>OASIS</strong><small>Equity-aware spatial decisions</small></span>
        </a>
        <span className="header-note">Auditable GIS agent - deterministic optimization</span>
      </header>
      <main>{children}</main>
      <footer>OASIS keeps model decisions observable and geospatial computation deterministic.</footer>
    </div>
  );
}
