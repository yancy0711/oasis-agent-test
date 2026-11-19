import type { SVGProps } from "react";

const paths = {
  moon: "M20.5 15.2A9 9 0 0 1 8.8 3.5 9 9 0 1 0 20.5 15.2Z",
  sun: "M12 3v2m0 14v2M3 12h2m14 0h2M5.6 5.6 7 7m10 10 1.4 1.4m0-12.8L17 7M7 17l-1.4 1.4M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z",
  map: "M3 6l6-3 6 3 6-3v15l-6 3-6-3-6 3V6zM9 3v15m6-12v15",
  layers: "M12 3l9 5-9 5-9-5 9-5zM3 12l9 5 9-5M3 16l9 5 9-5",
  setup: "M4 7h6m4 0h6M4 17h10m4 0h2M10 4v6m4 4v6",
  chart: "M4 3v17h17m-13-5 4-4 4 2 5-7",
  report: "M6 3h9l4 4v14H6zM14 3v5h5M9 12h7m-7 4h7",
  chat: "M21 11.5a8.5 8.5 0 0 1-8.5 8.5H4l1.8-3.8A8.5 8.5 0 1 1 21 11.5zM9 12h.01m3.5 0h.01m3.5 0h.01",
  left: "M4 4h16v16H4zM10 4v16",
  right: "M4 4h16v16H4zM14 4v16",
  upload: "M12 16V3m-4 4 4-4 4 4M4 15v5h16v-5",
  arrow: "M5 12h14m-6-6 6 6-6 6",
  send: "M12 19V5m-6 6 6-6 6 6",
  edit: "M4 20l4-.8L20 7l-3-3L5 16l-1 4zM14.5 6.5l3 3",
  close: "m6 6 12 12M18 6 6 18",
  pin: "M19 10c0 5-7 11-7 11S5 15 5 10a7 7 0 1 1 14 0zM14.5 10a2.5 2.5 0 1 1-5 0 2.5 2.5 0 0 1 5 0z",
  thermometer: "M10 14.8V5a2 2 0 0 1 4 0v9.8a4 4 0 1 1-4 0zM12 9v8",
  people: "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2m20 0v-2a4 4 0 0 0-3-4m-6-6a4 4 0 1 1-8 0 4 4 0 0 1 8 0zm6.5 2a3.5 3.5 0 0 0-2-5.8",
  check: "m5 12 4 4L19 6",
  info: "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0zM12 11v5m0-8h.01",
} satisfies Record<string, string>;

export function WorkspaceIcon({ name, ...props }: SVGProps<SVGSVGElement> & { name: keyof typeof paths }) {
  return <svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" {...props}><path d={paths[name]} /></svg>;
}
