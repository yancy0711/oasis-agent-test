import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { useAppStore } from "../../store/appStore";
import { LoadingText } from "../../components/LoadingText";
import { ResidentCandidates } from "./ResidentCandidates";
import { ResidentMap } from "./ResidentMap";
import { CATEGORIES, DEFAULT_PREFERENCES, residentPost, recommendedRoutes, type DisplayRoute,
  type Category, type Place, type Origin, type Preferences, type ResidentRoute, type SearchResult } from "./residentApi";

const EMPTY_PLACES: Place[] = [];

export interface ResidentRequest { id: string; prompt: string; locate?: boolean; preferences?: Preferences; }
interface Props { request: ResidentRequest | null; cancelVersion: number; onBusy: (busy: boolean) => void; onPreferencesChange?: (preferences: Preferences) => void;
  onCancelReady?: (cancel: (() => Promise<void>) | null) => void; }

export function ResidentWorkspace({ request, cancelVersion, onBusy, onPreferencesChange, onCancelReady }: Props) {
  const [preferences, setPreferences] = useState<Preferences>(DEFAULT_PREFERENCES);
  const [origin, setOrigin] = useState<Origin | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [choices, setChoices] = useState<Origin[]>([]);
  const [address, setAddress] = useState("");
  const [radius, setRadius] = useState(5);
  const [result, setResult] = useState<SearchResult | null>(null);
  const [routes, setRoutes] = useState<Record<string, ResidentRoute>>({});
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [activeRoute, setActiveRoute] = useState<ResidentRoute | null>(null);
  const [overview, setOverview] = useState(true);
  const [sidebarWidth, setSidebarWidth] = useState(25);
  const [busy, setBusy] = useState(false);
  const [picking, setPicking] = useState(false);
  useEffect(() => { onPreferencesChange?.(preferences); }, [preferences, onPreferencesChange]);
  const [status, setStatus] = useState("Enter an address, select a map point, or use your location.");
  const [error, setError] = useState("");
  const active = useRef<{ controller: AbortController; searchId?: string } | null>(null);
  const lastRequest = useRef<string | null>(null);
  const generation = useRef(0);
  const pendingIds = useRef(new Set<string>());
  const stopping = useRef<Promise<boolean> | null>(null);

  const cancel = useCallback(() => {
    generation.current += 1;
    const operation = active.current;
    active.current = null;
    operation?.controller.abort();
    if (operation?.searchId) pendingIds.current.add(operation.searchId);
    setBusy(false); onBusy(false);
    if (stopping.current) return stopping.current;
    const stopped = (async () => {
      try {
        for (const id of pendingIds.current) {
          await residentPost(`cancel/${id}`, {});
          pendingIds.current.delete(id);
        }
        return true;
      } catch {
        setError("Could not confirm that the request stopped. Select Stop and reset to retry.");
        return false;
      }
    })();
    stopping.current = stopped;
    void stopped.then(() => { if (stopping.current === stopped) stopping.current = null; });
    return stopped;
  }, [onBusy]);

  useEffect(() => {
    onCancelReady?.(async () => { if (!await cancel()) throw new Error("Could not stop the nearby request. Select Stop and reset and try again."); });
    return () => onCancelReady?.(null);
  }, [cancel, onCancelReady]);

  const trackedPost = useCallback(<T,>(path: string, body: object, signal: AbortSignal) => {
    const requestId = crypto.randomUUID();
    if (active.current?.controller.signal === signal) active.current.searchId = requestId;
    return residentPost<T>(path, { ...body, request_id: requestId }, signal);
  }, []);

  const perform = useCallback(async (action: (signal: AbortSignal) => Promise<void>) => {
    const stopped = cancel();
    const version = generation.current;
    if (!await stopped || version !== generation.current) return;
    const operation: NonNullable<typeof active.current> = { controller: new AbortController() };
    active.current = operation;
    setBusy(true); onBusy(true); setError("");
    try { await action(operation.controller.signal); }
    catch (reason) {
      if (active.current === operation && !operation.controller.signal.aborted) {
        if (operation.searchId) pendingIds.current.add(operation.searchId);
        setError(reason instanceof Error ? reason.message : "The request failed. Try again or enter a US address.");
        setStatus("Search stopped. Your inputs are kept.");
      }
    } finally {
      if (active.current === operation) { active.current = null; setBusy(false); onBusy(false); }
    }
  }, [cancel, onBusy]);

  useEffect(() => { cancel(); }, [cancelVersion, cancel]);
  useEffect(() => () => { lastRequest.current = null; cancel(); }, [cancel]);
  const resetResults = useCallback(() => { setResult(null); setRoutes({}); setActiveRoute(null); setSelectedId(null); setOverview(true); }, []);

  const stageOrigin = useCallback((next: Origin) => {
    setOrigin(next); setConfirmed(false); setChoices([]); resetResults();
    setStatus(next.country_code === "us" ? "Confirm this starting point before searching nearby places."
      : next.country_code ? `Your location is ${next.label}, outside the supported US area. Please enter a US address.`
        : "The country could not be verified. Enter a more specific US address.");
  }, [resetResults]);

  const locate = useCallback(async (signal: AbortSignal) => {
    setConfirmed(false); setOrigin(null); setChoices([]); resetResults();
    setStatus("Waiting for browser location permission...");
    if (!navigator.geolocation) throw new Error("Location is unavailable in this browser. Enter a US address.");
    const position = await new Promise<GeolocationPosition>((resolve, reject) => {
      const aborted = () => reject(new DOMException("Cancelled", "AbortError"));
      signal.addEventListener("abort", aborted, { once: true });
      navigator.geolocation.getCurrentPosition(
        (value) => { signal.removeEventListener("abort", aborted); resolve(value); },
        (reason) => { signal.removeEventListener("abort", aborted); reject(new Error(reason.code === 1
          ? "Location permission was denied. Enter a US address or allow location access and retry."
          : "Could not determine your location. Enter a US address or retry.")); },
        { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 },
      );
    });
    signal.throwIfAborted();
    setStatus("Checking the country of your browser location...");
    const next = await trackedPost<Origin>("origin", { longitude: position.coords.longitude, latitude: position.coords.latitude }, signal);
    signal.throwIfAborted(); stageOrigin(next);
  }, [resetResults, stageOrigin, trackedPost]);

  const resolveAddress = useCallback(async (query: string, signal: AbortSignal) => {
    setConfirmed(false); setOrigin(null); resetResults(); setChoices([]);
    setStatus("Finding the address or landmark...");
    const data = await trackedPost<{ candidates: Origin[]; message: string }>("origins", { query }, signal);
    signal.throwIfAborted();
    if (data.candidates.length === 1) stageOrigin(data.candidates[0]);
    else { setChoices(data.candidates); setStatus(data.candidates.length ? data.message : "No matching address found. Enter a more specific US address."); }
  }, [resetResults, stageOrigin, trackedPost]);

  const search = useCallback(async (point: Origin, prefs: Preferences, signal: AbortSignal) => {
    if (prefs.needs_clarification) { setStatus(prefs.clarification); return; }
    resetResults(); setStatus("Finding nearby places...");
    const data = await trackedPost<SearchResult>("search", {
      longitude: point.longitude, latitude: point.latitude, preferences: prefs, radius_km: radius,
    }, signal);
    signal.throwIfAborted(); data.candidates.sort((a, b) => a.duration_minutes - b.duration_minutes || a.id.localeCompare(b.id));
    setResult(data); setSelectedId(data.candidates[0]?.id ?? null); setStatus(data.message);
    const prepared: Record<string, ResidentRoute> = {};
    for (const place of data.candidates.slice(0, 3)) {
      setStatus(`${data.message} Comparing routes...`);
      try {
        const route = await trackedPost<ResidentRoute>("routes", { search_id: data.search_id, candidate_id: place.id }, signal);
        signal.throwIfAborted(); prepared[place.id] = route; setRoutes({ ...prepared });
        const fastest = recommendedRoutes(Object.values(prepared)).fastest;
        setActiveRoute(fastest ?? route); setSelectedId((fastest ?? route).summary.destination_id);
      } catch (reason) {
        signal.throwIfAborted();
        setError(reason instanceof Error ? `Places are ready. Route unavailable: ${reason.message}` : "Places are ready; a route could not be loaded.");
        break; // Do not multiply requests during a provider outage.
      }
    }
    setStatus(data.message);
  }, [radius, resetResults, trackedPost]);

  const handleRequest = useCallback(async (input: ResidentRequest, signal: AbortSignal) => {
    if (input.locate) { await locate(signal); return; }
    setStatus("Understanding your nearby-place preferences...");
    const next = input.preferences ?? await residentPost<Preferences>("parse", {
      prompt: input.prompt, previous: preferences, llm_session_id: useAppStore.getState().llmSessionId,
    }, signal);
    signal.throwIfAborted(); setPreferences(next); resetResults();
    if (next.origin_query && (!origin || next.origin_query !== preferences.origin_query)) {
      setAddress(next.origin_query); await resolveAddress(next.origin_query, signal);
    } else if (next.use_current_location && !next.origin_query) { await locate(signal); }
    else if (origin && confirmed && origin.country_code === "us") await search(origin, next, signal);
    else if (next.origin_query) { setAddress(next.origin_query); await resolveAddress(next.origin_query, signal); }
    else await locate(signal);
  }, [preferences, origin, confirmed, locate, resolveAddress, search, resetResults]);

  useEffect(() => {
    if (!request || lastRequest.current === request.id) return;
    lastRequest.current = request.id;
    void perform((signal) => handleRequest(request, signal));
  }, [request, handleRequest, perform]);

  function updatePreferences(patch: Partial<Preferences>) {
    cancel(); resetResults(); setError("");
    setPreferences((current) => ({ ...current, ...patch }));
    setStatus("Filters updated. Select Update results beside the map to apply your filters.");
  }
  const validFilters = Number(preferences.threshold_minutes) > 0 && Number(preferences.threshold_minutes) <= 120
    && radius >= 1 && radius <= 30 && (preferences.categories ?? []).some((key) => !(preferences.excluded_categories ?? []).includes(key));

  function confirmOrigin(point: Origin) {
    stageOrigin(point);
    if (point.country_code !== "us") return;
    setConfirmed(true);
    if (!preferences.needs_clarification && validFilters) void perform((signal) => search(point, preferences, signal));
  }
  function navigate(candidateId: string) {
    cancel(); setOverview(false); setSelectedId(candidateId); setActiveRoute(null); setError("");
    if (!result) return;
    if (routes[candidateId]) { setActiveRoute(routes[candidateId]); setStatus(result.message); return; }
    void perform(async (signal) => {
      setStatus("Planning the route to the selected place...");
      const route = await trackedPost<ResidentRoute>("routes", { search_id: result.search_id, candidate_id: candidateId }, signal);
      signal.throwIfAborted(); setRoutes((current) => ({ ...current, [candidateId]: route })); setActiveRoute(route);
      setStatus(result.message);
    });
  }
  const recommendations = useMemo(() => recommendedRoutes(Object.values(routes)), [routes]);
  const displayedRoutes = useMemo<DisplayRoute[]>(() => overview ? [
    ...(recommendations.fastest ? [{ route: recommendations.fastest, variant: "fastest" as const }] : []),
    ...(recommendations.greener ? [{ route: recommendations.greener, variant: "greener" as const }] : []),
  ] : activeRoute ? [{ route: activeRoute, variant: "selected" }] : [], [overview, recommendations, activeRoute]);

  const included = preferences.categories ?? [];
  const excluded = preferences.excluded_categories ?? [];
  const outside = !!origin?.country_code && origin.country_code !== "us";
  const retryIncomplete = result && !result.complete
    && result.checked_count < Math.min(result.discovered_count, result.candidate_limit ?? 50);
  const searchBlocker = busy ? "Please wait for the current request, or cancel it."
    : outside ? "Enter a US address or choose a point in the United States."
    : !origin || !confirmed ? "Choose and confirm a starting point beside the map."
    : !validFilters ? "Choose at least one category, a travel time of 1-120 minutes and a radius of 1-30 km."
    : preferences.needs_clarification ? preferences.clarification || "Clarify your request in the prompt above." : "";
  return <section className="panel resident-workspace" aria-label="Resident nearby places">
    <div className="section-heading"><div><p className="eyebrow">For residents</p><h2>Find nearby places</h2></div>
      <span className="status">Search and navigate</span></div>
    <p>Find reachable places to cool down.</p>
    <form className="inline-form resident-origin-controls" onSubmit={(event) => { event.preventDefault(); if (address.trim()) void perform((signal) => resolveAddress(address.trim(), signal)); }}>
      <label htmlFor="resident-address">US address or landmark</label>
      <input id="resident-address" value={address} onChange={(event) => { cancel(); setAddress(event.target.value); setConfirmed(false); resetResults(); }} placeholder="Riverside City Hall, California" />
      <button disabled={busy || address.trim().length < 2}>Find address</button>
      <button type="button" disabled={busy} onClick={() => { setPicking(false); void perform(locate); }}>Use my location</button>
    </form>
    {choices.length > 0 && <div className="candidate-list">{choices.map((point, index) => <button className="candidate-card" key={index} disabled={busy} onClick={() => confirmOrigin(point)}>
      <strong>{point.label}</strong><span>{point.country_code === "us" ? "Confirm and search from here" : "Show this location (outside US)"}</span>
    </button>)}</div>}
    <div className="form-grid">
      <label className="field">Travel mode<select aria-label="Resident travel mode" value={preferences.mode} onChange={(event) => updatePreferences({ mode: event.target.value as Preferences["mode"] })}>
        <option value="walking">Walking</option><option value="cycling">Cycling</option><option value="driving">Driving</option>
      </select></label>
      <label className="field">Travel time (minutes)<input aria-label="Resident travel time" type="number" min={1} max={120} value={preferences.threshold_minutes} onChange={(event) => updatePreferences({ threshold_minutes: Number(event.target.value) })} /></label>
      <label className="field">Search radius (km)<input aria-label="Search radius" type="number" min={1} max={30} value={radius} onChange={(event) => { cancel(); resetResults(); setError(""); setRadius(Number(event.target.value)); setStatus("Filters updated. Select Update results beside the map."); }} /></label>
    </div>
    <fieldset className="resident-categories"><legend>Place categories</legend>
      <label><input type="checkbox" aria-label="Select all place categories"
        checked={Object.keys(CATEGORIES).every((key) => included.includes(key as Category) && !excluded.includes(key as Category))}
        onChange={(event) => updatePreferences({ categories: event.target.checked ? Object.keys(CATEGORIES) as Category[] : [], excluded_categories: [] })} />Select all</label>
      {Object.entries(CATEGORIES).map(([key, value]) => {
      const category = key as Category;
      return <label key={category}><input type="checkbox" checked={included.includes(category) && !excluded.includes(category)} onChange={(event) => updatePreferences({
        categories: event.target.checked ? [...new Set([...included, category])] : included.filter((item) => item !== category),
        excluded_categories: event.target.checked ? excluded.filter((item) => item !== category) : [...new Set([...excluded, category])],
      })} /><span className="resident-category-dot" style={{ background: value.color }} />{value.label}</label>;
    })}</fieldset>
    {excluded.length > 0 && <p>Excluded: {excluded.map((key) => CATEGORIES[key].label).join(", ")}</p>}
    {!!preferences.preferred_categories?.length && <p>Preferred: {preferences.preferred_categories.map((key) => CATEGORIES[key].label).join(", ")}</p>}
    {preferences.needs_clarification && <button disabled={busy} onClick={() => updatePreferences({ needs_clarification: false, clarification: "" })}>Use the displayed supported filters</button>}
    <div className="resident-explorer" style={{ "--resident-sidebar": `${sidebarWidth}%` } as CSSProperties}>
      <ResidentCandidates emptyMessage={busy ? "Matching places will appear here." : confirmed ? (error ? "Search failed. Your starting point is confirmed; retry beside the map." : "Your starting point is confirmed. Update results to apply your filters.") : "Choose and confirm a starting point to find places here."} result={result} selectedId={selectedId} fastestId={recommendations.fastest?.summary.destination_id ?? null} preferred={preferences.preferred_categories ?? []} onSelect={navigate} onNavigate={navigate} />
      <div className="resident-resizer" role="separator" tabIndex={0} aria-label="Resize place list" aria-orientation="vertical"
        aria-valuemin={20} aria-valuemax={55} aria-valuenow={sidebarWidth}
        onDoubleClick={() => setSidebarWidth(25)}
        onKeyDown={(event) => {
          if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
            event.preventDefault(); setSidebarWidth((width) => Math.max(20, Math.min(55, width + (event.key === "ArrowLeft" ? -2 : 2))));
          }
        }}
        onPointerDown={(event) => { event.preventDefault(); event.currentTarget.setPointerCapture(event.pointerId); }}
        onPointerMove={(event) => {
          if (!event.currentTarget.hasPointerCapture(event.pointerId)) return;
          const bounds = event.currentTarget.parentElement!.getBoundingClientRect();
          setSidebarWidth(Math.round(Math.max(20, Math.min(55, (event.clientX - bounds.left) / bounds.width * 100))));
        }}
        onPointerUp={(event) => event.currentTarget.releasePointerCapture(event.pointerId)} />
      <div className="resident-map-column">
    {origin && <div className={outside ? "resident-location-warning" : "notice"} role={outside ? "alert" : "status"}>
      <strong>{outside ? "Outside supported area" : confirmed ? "Confirmed starting point" : "Confirm your starting point"}</strong>
      <p>{origin.label} ({origin.latitude.toFixed(5)}, {origin.longitude.toFixed(5)})</p>
      {outside ? <p>This location is outside the United States. Please enter a US address above.</p>
        : origin.country_code === "us" && !confirmed && <button disabled={busy} onClick={() => confirmOrigin(origin)}>Confirm location and search</button>}
    </div>}
      <div className="button-row resident-search-actions">
        {confirmed && !busy && (!result || retryIncomplete) && <button aria-describedby="resident-search-help" disabled={!!searchBlocker || picking}
          onClick={() => origin && void perform((signal) => search(origin, preferences, signal))}>{error ? "Retry search" : result ? "Retry incomplete search" : "Update results"}</button>}
        {(busy || error) && <button className="button--danger" onClick={async () => {
          if (await cancel()) { resetResults(); setError(""); setStatus("Request cancelled. Your location and filters are kept."); }
        }}>Stop and reset</button>}
      </div>
      <div className="resident-request-status">
        {searchBlocker && <p id="resident-search-help">{searchBlocker}</p>}
        {(preferences.notice || (!preferences.needs_clarification && preferences.clarification)) && <p className="notice">{preferences.notice || preferences.clarification}</p>}
        <p role="status" aria-busy={busy}><LoadingText active={busy}>{status}</LoadingText></p>
        {error && <p className="error" role="alert">{error}</p>}
      </div>
      <ResidentMap selectedId={selectedId} fastestId={recommendations.fastest?.summary.destination_id ?? null} mode={result?.mode ?? preferences.mode} showPlacePopup={!overview} picking={picking} onPickingChange={(next) => { if (next) cancel(); setPicking(next); }} origin={origin} places={result?.candidates ?? EMPTY_PLACES} routes={displayedRoutes} onSelect={navigate}
        onOrigin={(longitude, latitude) => void perform(async (signal) => {
          setConfirmed(false); setOrigin(null); resetResults();
          setStatus("Checking the selected map point...");
          const next = await trackedPost<Origin>("origin", { longitude, latitude }, signal);
          signal.throwIfAborted(); setAddress(`${latitude.toFixed(5)}, ${longitude.toFixed(5)}`); stageOrigin(next);
        })} />
      </div>
    </div>
    {activeRoute && <div className="notice resident-route-summary"><strong>Route to {activeRoute.summary.destination_name}</strong>
      <p>{activeRoute.summary.duration_minutes.toFixed(1)} min / {activeRoute.summary.distance_km.toFixed(2)} km
        {activeRoute.summary.green_score != null && ` / Green index ${activeRoute.summary.green_score.toFixed(1)}/10`}</p>
      {!activeRoute.summary.within_threshold && <p className="error">The detailed route exceeds your time limit. Choose another place or revise the limit.</p>}
      </div>}
    {overview && result && Object.keys(routes).length > 0 && <p className="notice">
      Compared {Object.keys(routes).length} nearby routes. {recommendations.greener ? "Solid: fastest. Green dashed: greener." : "Showing the fastest compared route."}
    </p>}
    <details className="resident-data-notes"><summary>About these results</summary>
      <p>Potential cooling places, not verified cooling centers. Opening hours and access may be unknown.</p>
      <p>Routes use ORS travel times. Up to three nearby destinations are compared. The distance-weighted ORS green index
        is available for some walking routes; it measures neither temperature nor shade.</p>
      <p>Up to 50 candidates nearest by straight-line distance are checked, then ordered by network travel time.
        Narrow the radius when the search is limited.</p>
      {activeRoute && <p>{activeRoute.summary.message}</p>}
    </details>
  </section>;
}
