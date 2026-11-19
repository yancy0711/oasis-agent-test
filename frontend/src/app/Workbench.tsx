import { lazy, Suspense, useLayoutEffect, useRef, useState, type CSSProperties, type DragEvent, type KeyboardEvent, type ReactNode } from "react";
import { useAppStore } from "../store/appStore";
import { RunWorkspace } from "../features/run/RunWorkspace";
import { WorkspaceDialog } from "./WorkspaceDialog";
import { WorkspaceIcon } from "./WorkspaceIcon";
import { PaneDivider } from "./PaneDivider";
import { useWorkspacePreferences } from "./WorkspacePreferences";
import type { StudyInputLayers } from "../api/models";

const StudyMap = lazy(() => import("./StudyMap").then((module) => ({ default: module.StudyMap })));
const tabs = [
  { label: "Data", icon: "layers" },
  { label: "Setup", icon: "setup" },
  { label: "Results", icon: "chart" },
  { label: "Report", icon: "report" },
] as const;

export type WorkspaceTab = (typeof tabs)[number]["label"];

export function Workbench({ activeTab, onTabChange, setup, chat, settings, upload, onUpload, onDemandDrop, uploadPending, resident, place, taskPending, connection, utciConfigured, onResult, populationLayer, inputLayers, uploadedRun, previewDraft }: {
  activeTab: WorkspaceTab;
  onTabChange: (tab: WorkspaceTab) => void;
  setup: ReactNode;
  chat: (runActivity: ReactNode, showUpload: () => void) => ReactNode;
  settings: ReactNode;
  upload: ReactNode;
  onUpload: (file: File) => void;
  onDemandDrop: (file: File) => void;
  uploadPending: boolean;
  resident: ReactNode;
  place?: string;
  taskPending: boolean;
  connection: "connected" | "unavailable" | "connecting";
  utciConfigured?: boolean;
  populationLayer?: ReactNode;
  inputLayers: StudyInputLayers;
  uploadedRun?: boolean;
  previewDraft?: boolean;
  onResult: () => void;
}) {
  const confirmed = useAppStore((state) => state.confirmedPlace);
  const { t, theme, language, setTheme, setLanguage } = useWorkspacePreferences();
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [chatOpen, setChatOpen] = useState(true);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [runSlot, setRunSlot] = useState<HTMLDivElement | null>(null);
  const [resultSlot, setResultSlot] = useState<HTMLDivElement | null>(null);
  const [reportSlot, setReportSlot] = useState<HTMLDivElement | null>(null);
  const [mapSlot, setMapSlot] = useState<HTMLDivElement | null>(null);
  const [hasResultMap, setHasResultMap] = useState(false);
  const [dataSlot, setDataSlot] = useState<HTMLDivElement | null>(null);
  const [overlaySlot, setOverlaySlot] = useState<HTMLDivElement | null>(null);
  const [dragging, setDragging] = useState(false);
  const [dropMessage, setDropMessage] = useState("");
  const dragDepth = useRef(0);
  const tabList = useRef<HTMLDivElement>(null);
  const uploadArea = useRef<HTMLDivElement>(null);
  const inspector = useRef<HTMLElement>(null);
  const inspectorBody = useRef<HTMLDivElement>(null);
  const assistant = useRef<HTMLElement>(null);
  const layout = useRef<HTMLDivElement>(null);
  const [layoutWidth, setLayoutWidth] = useState(() => window.innerWidth);
  const [preferredLeft, setPreferredLeft] = useState<number | null>(null);
  const [preferredRight, setPreferredRight] = useState<number | null>(null);
  useLayoutEffect(() => {
    const update = () => setHasResultMap(Boolean(mapSlot?.childElementCount));
    update();
    if (!mapSlot) return;
    const observer = new MutationObserver(update);
    observer.observe(mapSlot, { childList: true });
    return () => observer.disconnect();
  }, [mapSlot]);
  useLayoutEffect(() => {
    const element = layout.current;
    if (!element) return;
    const observer = new ResizeObserver(() => setLayoutWidth(element.clientWidth));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const leftMin = inspectorOpen && !resident ? 220 : resident ? 0 : 48;
  const rightMin = chatOpen ? 250 : 48;
  const leftTarget = inspectorOpen && !resident ? preferredLeft ?? Math.max(252, Math.min(360, layoutWidth / 4)) : leftMin;
  const rightTarget = chatOpen ? preferredRight ?? Math.max(310, Math.min(360, layoutWidth / 4)) : rightMin;
  const sideSpace = Math.max(0, layoutWidth - 300 - leftMin - rightMin);
  const extra = Math.max(1, leftTarget + rightTarget - leftMin - rightMin);
  const ratio = Math.min(1, sideSpace / extra);
  const leftWidth = Math.round(leftMin + (leftTarget - leftMin) * ratio);
  const rightWidth = Math.round(rightMin + (rightTarget - rightMin) * ratio);
  const layoutStyle = { "--inspector-width": `${leftWidth}px`, "--chat-width": `${rightWidth}px` } as CSSProperties;
  const area = confirmed?.canonical_name || place;
  const showDemandPreview = Boolean(previewDraft && (activeTab === "Data" || activeTab === "Setup"));
  const utciStatus = connection === "unavailable" ? "ERA5-HEAT status unavailable"
    : connection === "connecting" ? "Checking ERA5-HEAT configuration"
      : utciConfigured ? "ERA5-HEAT source configured" : "No ERA5-HEAT source configured";
  useLayoutEffect(() => {
    setInspectorOpen(true);
    inspectorBody.current?.scrollTo({ top: 0 });
    if (activeTab !== "Data" && window.matchMedia("(max-width: 820px)").matches) inspector.current?.scrollIntoView({ block: "start" });
  }, [activeTab]);

  function editTask() {
    setInspectorOpen(true);
    onTabChange("Setup");
  }

  function showUpload() {
    setInspectorOpen(true);
    onTabChange("Data");
    requestAnimationFrame(() => uploadArea.current?.querySelector("input")?.focus());
  }

  function openAssistant() {
    setChatOpen(true);
    requestAnimationFrame(() => assistant.current?.querySelector("textarea")?.focus());
  }

  function acceptFileDrop(event: DragEvent) {
    if (!event.dataTransfer.types.includes("Files")) return;
    event.preventDefault();
    dragDepth.current = 0;
    setDragging(false);
    if (uploadPending) return;
    setInspectorOpen(true);
    onTabChange("Data");
    const files = event.dataTransfer.files;
    const file = files[0];
    if (files.length !== 1 || !/\.(csv|geojson)$/i.test(file.name)) {
      setDropMessage("Drop one candidate CSV or demand GeoJSON file.");
      return;
    }
    setDropMessage("");
    if (/\.geojson$/i.test(file.name)) onDemandDrop(file);
    else onUpload(file);
  }

  const fileDropEvents = {
    onDragEnter: (event: DragEvent) => {
      if (!event.dataTransfer.types.includes("Files")) return;
      event.preventDefault();
      dragDepth.current += 1;
      setDragging(true);
    },
    onDragLeave: (event: DragEvent) => {
      if (!event.dataTransfer.types.includes("Files")) return;
      dragDepth.current = Math.max(dragDepth.current - 1, 0);
      if (!dragDepth.current) setDragging(false);
    },
    onDragOver: (event: DragEvent) => {
      if (event.dataTransfer.types.includes("Files")) event.preventDefault();
    },
    onDrop: acceptFileDrop,
  };

  function navigateTabs(event: KeyboardEvent<HTMLButtonElement>, current: number) {
    const next = { ArrowRight: (current + 1) % tabs.length, ArrowLeft: (current + tabs.length - 1) % tabs.length, Home: 0, End: tabs.length - 1 }[event.key];
    if (next === undefined) return;
    event.preventDefault();
    onTabChange(tabs[next].label);
    tabList.current?.querySelectorAll("button")[next]?.focus();
  }

  return <div className="workbench" data-theme={theme}>
    <header className="workbench-header">
      <a className="workbench-brand" href="/" aria-label="OASIS home">
        <span className="workbench-brand__mark"><WorkspaceIcon name="map" /></span>
        <strong>OASIS</strong>
      </a>
      <span className="workbench-header__title">{t("Cooling Center Siting")}</span>
      <span className="workbench-area-chip"><WorkspaceIcon name="pin" />{area || t("United States")}</span>
      <span className={`workbench-connection workbench-connection--${connection}`} role="status"><i aria-hidden="true" />{t(connection === "connected" ? "Backend connected" : connection === "unavailable" ? "Backend unavailable" : "Connecting")}</span>
      <div className="workbench-header__actions">
        <button type="button" className="workbench-settings-button" aria-label={t("Settings")} onClick={() => setSettingsOpen(true)}><WorkspaceIcon name="setup" /><span>{t("Model settings")}</span></button>
        <button type="button" className="workbench-settings-button workbench-preference" aria-label={t("Switch language")} title={t("Switch language")} onClick={() => setLanguage(language === "en" ? "zh-CN" : "en")}>CN/EN</button>
        <button type="button" className="workbench-settings-button workbench-preference" aria-label={t(theme === "light" ? "Switch to dark mode" : "Switch to light mode")} title={t(theme === "light" ? "Switch to dark mode" : "Switch to light mode")}
          onClick={() => setTheme(theme === "light" ? "dark" : "light")}><WorkspaceIcon name={theme === "light" ? "moon" : "sun"} /></button>
      </div>
    </header>

    <div ref={layout} style={layoutStyle} className="workbench-layout" data-inspector={inspectorOpen ? "open" : "closed"} data-chat={chatOpen ? "open" : "closed"} data-resident={Boolean(resident)}>
      {!resident && <aside ref={inspector} id="workspace-inspector" className="workbench-inspector" aria-label={t("Workspace")}>
        <div className="workbench-panel-heading"><h2>{t("Workspace")}</h2>
          <button type="button" className="workbench-icon-button" aria-label={t(inspectorOpen ? "Collapse workspace panel" : "Expand workspace panel")} title={t("Workspace")} aria-controls="workspace-inspector" aria-expanded={inspectorOpen} onClick={() => setInspectorOpen(!inspectorOpen)}><WorkspaceIcon name="left" /></button>
        </div>
        <div className="workbench-tabs" role="tablist" aria-label={t("Workspace views")} ref={tabList}>
          {tabs.map(({ label, icon }, index) => <button type="button" key={label} role="tab"
            id={`workspace-tab-${label}`} aria-controls={`workspace-view-${label}`} aria-selected={activeTab === label}
            tabIndex={activeTab === label ? 0 : -1} onClick={() => { setInspectorOpen(true); onTabChange(label); }}
            onKeyDown={(event) => navigateTabs(event, index)} title={t(label)} aria-label={t(label)}>
            <WorkspaceIcon name={icon} /><span>{t(label)}</span>
          </button>)}
        </div>
        <div ref={inspectorBody} className="workbench-inspector__body">
          <section id="workspace-view-Data" role="tabpanel" aria-labelledby="workspace-tab-Data" hidden={activeTab !== "Data"}>
            <div className="workbench-card workbench-study-area">
              <span className="workbench-layer-icon"><WorkspaceIcon name="pin" /></span>
              <div><strong>{area || t("Study area")}</strong><p>{t(confirmed ? "Confirmed boundary" : area ? "Boundary needs confirmation" : "Choose an area with a prompt")}</p></div>
              {confirmed && <span className="workbench-confirmed" title={t("Confirmed")}><WorkspaceIcon name="check" /><span className="workbench-visually-hidden">{t("Confirmed")}</span></span>}
            </div>
            <div className="workbench-upload-area" ref={uploadArea} onChangeCapture={() => setDropMessage("")} {...fileDropEvents}>{upload}{dropMessage && <p className="error" role="alert">{t(dropMessage)}</p>}</div>
            <div className="workbench-data-heading"><h3>{t("Input layers")}</h3><span>{t("Sources used at run time")}</span></div>
            <ul className="workbench-layer-list">
              <li><span className="workbench-layer-icon"><WorkspaceIcon name="people" /></span><div><strong>{t("Population & vulnerability")}</strong>{populationLayer ?? <small>{t("US Census, Hong Kong Census, or uploaded demand")}</small>}</div></li>
              <li><span className="workbench-layer-icon workbench-layer-icon--heat"><WorkspaceIcon name="thermometer" /></span><div><strong>{t("Heat exposure")}</strong><small>{t("ERA5-HEAT UTCI and Landsat LST")}</small></div></li>
              <li><span className="workbench-layer-icon"><WorkspaceIcon name="pin" /></span><div><strong>{t("Candidate facilities")}</strong><small>{t("Configured sources or uploaded CSV")}</small></div></li>
              <li><span className="workbench-layer-icon"><WorkspaceIcon name="map" /></span><div><strong>{t("Travel times")}</strong><small>{t("From the confirmed routing mode")}</small></div></li>
            </ul>
            <details className="workbench-disclosure" open={connection === "connected" && !utciConfigured ? true : undefined}>
              <summary>ERA5-HEAT UTCI · {t(utciStatus)}</summary>
              {connection === "connected" && !utciConfigured && <p><a href="https://cds.climate.copernicus.eu/how-to-api" target="_blank" rel="noreferrer">{t("Connect Copernicus CDS")}</a> {t("or configure a local NetCDF source.")}</p>}
              <p>{t("After a new run, switch the heat layer in the result map.")}</p>
            </details>
            <details className="workbench-disclosure"><summary>{t("Data checks & processing")}</summary>
              <div className="workbench-data-slot" ref={setDataSlot} />
              <p>{t("See preparation steps in Run activity and source records in Report.")}</p>
            </details>
          </section>
          <section id="workspace-view-Setup" role="tabpanel" aria-labelledby="workspace-tab-Setup" hidden={activeTab !== "Setup"}>
            {setup || <p className="workbench-description">{t(taskPending ? "Waiting for a parsed task…" : "Analyze a prompt to review and confirm the task.")}</p>}
          </section>
          <section id="workspace-view-Results" role="tabpanel" aria-labelledby="workspace-tab-Results" hidden={activeTab !== "Results"}>
            <div className="workbench-result-slot" ref={setResultSlot} />
          </section>
          <section id="workspace-view-Report" role="tabpanel" aria-labelledby="workspace-tab-Report" hidden={activeTab !== "Report"}>
            <div className="workbench-report-slot" ref={setReportSlot} />
            <p className="workbench-description workbench-slot-empty">{t("The report and data provenance appear after a result is available.")}</p>
          </section>
        </div>
        {inspectorOpen && <PaneDivider side="left" pane={inspector} min={leftMin}
          max={Math.max(leftMin, layoutWidth - rightWidth - 300)} value={leftWidth}
          onResize={(width) => { if (chatOpen) setPreferredRight(rightWidth); setPreferredLeft(width); }} />}
      </aside>}

      <main className="workbench-main" aria-label={t(resident ? "Nearby-facility workspace" : "Map workspace")}>
        {resident || <>
          <div className="workbench-map" {...fileDropEvents}>
            <div className="workbench-map-slot" ref={setMapSlot} hidden={showDemandPreview} />
            {(!hasResultMap || showDemandPreview) && <div className="study-map">
              <Suspense fallback={null}><StudyMap inputLayers={inputLayers} uploadedRun={uploadedRun} place={area} /></Suspense>
              <div className="study-map__note">
                <span className="workbench-layer-icon"><WorkspaceIcon name="map" /></span>
                <div><strong>{area || t("No study area selected")}</strong><p>{t(area ? "Run the confirmed task to load analysis layers." : "Start with a city or county in the assistant.")}</p></div>
                {!area && <button type="button" onClick={openAssistant}>{t("Ask")}</button>}
              </div>
            </div>}
            {dragging && <div className="workbench-drop-overlay">{t(uploadPending ? "Uploading candidate CSV" : "Drop a candidate CSV or demand GeoJSON")}<WorkspaceIcon name="upload" /></div>}
          </div>
        </>}
      </main>

      <aside ref={assistant} id="workspace-assistant" className="workbench-assistant" aria-label={t("Assistant")}>
        <div className="workbench-panel-heading"><h2>{t("Assistant")}</h2>
          <button type="button" className="workbench-icon-button" aria-label={t(chatOpen ? "Collapse assistant panel" : "Expand assistant panel")} title={t("Assistant")} aria-controls="workspace-assistant" aria-expanded={chatOpen} onClick={() => setChatOpen(!chatOpen)}><WorkspaceIcon name="right" /></button>
        </div>
        {chat(<div className="workbench-run-slot" ref={setRunSlot} />, showUpload)}
        {chatOpen && <PaneDivider side="right" pane={assistant} min={rightMin}
          max={Math.max(rightMin, layoutWidth - leftWidth - 300)} value={rightWidth}
          onResize={(width) => { if (inspectorOpen && !resident) setPreferredLeft(leftWidth); setPreferredRight(width); }} />}
      </aside>
    </div>

    <div ref={setOverlaySlot} className="workbench-overlay-slot" />
    {!resident && <RunWorkspace slots={{ run: runSlot, result: resultSlot, report: reportSlot, map: mapSlot, data: dataSlot, overlay: overlaySlot }} onResult={onResult} onEditTask={editTask} />}
    <WorkspaceDialog title={t("Settings")} open={settingsOpen} onClose={() => setSettingsOpen(false)}>{settings}</WorkspaceDialog>
  </div>;
}
