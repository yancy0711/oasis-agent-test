import { getStreetView } from "../../api/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render as renderUI, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ResidentWorkspace, type ResidentRequest } from "./ResidentWorkspace";
import { DEFAULT_PREFERENCES, residentPost, type Origin, type Preferences, type ResidentRoute, type SearchResult, type DisplayRoute } from "./residentApi";

vi.mock("./ResidentMap", () => ({ ResidentMap: ({ origin, routes }: { origin: Origin | null; routes: DisplayRoute[] }) =>
  <div aria-label="Test map" data-routes={routes.map((item) => `${item.variant}:${item.route.summary.destination_id}`).join(",")}>{origin?.country_code === "hk" ? "Outside marker: Hong Kong" : origin?.label}</div> }));
vi.mock("./residentApi", async (original) => ({ ...await original<typeof import("./residentApi")>(), residentPost: vi.fn() }));
vi.mock("../../api/client", () => ({ getStreetView: vi.fn(async () => ({ available: false, status: "no_image", message: "No nearby image." })) }));
function render(ui: Parameters<typeof renderUI>[0]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderUI(ui, { wrapper: ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider> });
}
const post = vi.mocked(residentPost);
const origin: Origin = { longitude: -117.375, latitude: 33.981, label: "Riverside City Hall", country_code: "us" };
const prefs: Preferences = { ...DEFAULT_PREFERENCES, use_current_location: false, origin_query: null,
  categories: ["mall"], excluded_categories: ["park"], threshold_minutes: 10 };
const result: SearchResult = { search_id: "search-1", origin, mode: "walking", threshold_minutes: 10, radius_km: 5,
  complete: true, candidate_limit: 50, discovered_count: 1, checked_count: 1, message: "One matching mall.", source: "OSM / ORS",
  candidates: [{ id: "way/1", name: "Test Mall", category: "mall", longitude: -117.37, latitude: 33.98,
    duration_minutes: 6, access: "yes", fee: "no", indoor: "yes", opening_hours: "Unknown",
    coordinate_source: "Mapped entrance", osm_url: "https://www.openstreetmap.org/way/1" }] };
const route: ResidentRoute = { type: "FeatureCollection", features: [], summary: { destination_id: "way/1", destination_name: "Test Mall",
  duration_minutes: 6, distance_km: 0.5, within_threshold: true, heat_status: "unavailable", message: "Heat estimates unavailable." } };
const locate = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  Element.prototype.scrollIntoView = vi.fn();
  Object.defineProperty(navigator, "geolocation", { configurable: true, value: { getCurrentPosition: locate } });
  locate.mockImplementation((success) => success({ coords: { longitude: origin.longitude, latitude: origin.latitude } }));
  post.mockImplementation(async (path) => {
    if (path === "parse") return prefs;
    if (path === "origin") return origin;
    if (path === "origins") return { candidates: [origin], message: "Confirm" };
    if (path === "search") return result;
    if (path === "routes") return route;
    if (path.startsWith("cancel/")) return {};
    throw new Error(`Unexpected request: ${path}`);
  });
});
afterEach(cleanup);

function start(request: ResidentRequest = { id: "input-1", prompt: "Find malls near me, no parks" }) {
  const onBusy = vi.fn();
  const view = render(<ResidentWorkspace request={null} cancelVersion={0} onBusy={onBusy} />);
  view.rerender(<ResidentWorkspace request={request} cancelVersion={0} onBusy={onBusy} />);
  return { ...view, onBusy };
}

describe("resident discovery", () => {
  it("defaults to all categories without a prompt restriction", () => {
    render(<ResidentWorkspace request={null} cancelVersion={0} onBusy={vi.fn()} />);
    expect(screen.getByRole("checkbox", { name: "Select all place categories" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Parks" })).toBeChecked();
  });
  it("does not offer an identical retry when the nearest-candidate limit was reached", async () => {
    const original = post.getMockImplementation()!;
    post.mockImplementation(async (...args) => args[0] === "search" ? {
      ...result, complete: false, candidate_limit: 50, checked_count: 50, discovered_count: 200,
      message: "Partial results: checked 50 of 200 places.",
    } : original(...args));
    start();
    fireEvent.click(await screen.findByRole("button", { name: "Confirm location and search" }));
    await screen.findByText("Route to Test Mall");
    expect(screen.queryByRole("button", { name: "Retry incomplete search" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Partial results (1)" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Search radius"), { target: { value: "2" } });
    expect(screen.getByRole("button", { name: "Update results" })).toBeEnabled();
  });

  it("selects all categories, clears prompt exclusions and applies only when updating", async () => {
    start();
    fireEvent.click(await screen.findByRole("button", { name: "Confirm location and search" }));
    await screen.findByText("Route to Test Mall");
    fireEvent.click(screen.getByRole("checkbox", { name: "Select all place categories" }));
    expect(screen.getByRole("checkbox", { name: "Parks" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Public libraries" })).toBeChecked();
    expect(post.mock.calls.filter(([path]) => path === "search")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Update results" }));
    await screen.findByText("Route to Test Mall");
    const requests = post.mock.calls.filter(([path]) => path === "search");
    const latest = requests.at(-1)![1] as { preferences: Preferences };
    expect(latest.preferences.categories).toHaveLength(8);
    expect(latest.preferences.excluded_categories).toEqual([]);
    expect(locate).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("checkbox", { name: "Select all place categories" }));
    expect(screen.getByRole("button", { name: "Update results" })).toBeDisabled();
  });

  it("locates a missing origin, requires confirmation and applies prompt categories without a budget", async () => {
    start();
    await screen.findByRole("button", { name: "Confirm location and search" });
    expect(locate).toHaveBeenCalledOnce();
    expect(post.mock.calls.some(([path]) => path === "search")).toBe(false);
    expect(screen.getByRole("checkbox", { name: "Shopping malls" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Parks" })).not.toBeChecked();
    expect(screen.queryByLabelText("Facilities")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm location and search" }));
    await screen.findByRole("button", { name: "Route to Test Mall" });
    await screen.findByText("Route to Test Mall");
    const search = post.mock.calls.find(([path]) => path === "search")?.[1];
    expect(search).toMatchObject({ longitude: origin.longitude, preferences: { categories: ["mall"], excluded_categories: ["park"], threshold_minutes: 10 } });
    expect(search).not.toHaveProperty("n_facilities");
  });

  it("shows an outside-US coordinate and never searches there", async () => {
    post.mockImplementation(async (path) => path === "origin" ? { ...origin, longitude: 114.17, latitude: 22.3, country_code: "hk", label: "Hong Kong" } : prefs);
    start({ id: "locate-1", prompt: "", locate: true });
    await screen.findByText("Outside supported area");
    expect(screen.getByText("Outside marker: Hong Kong")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Update results" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("US address or landmark")).toBeEnabled();
    expect(post.mock.calls.some(([path]) => path === "search")).toBe(false);
  });

  it("keeps manual address entry available after permission denial", async () => {
    locate.mockImplementation((_success, failure) => failure({ code: 1 }));
    start({ id: "denied", prompt: "", locate: true });
    await screen.findByText(/Location permission was denied/);
    fireEvent.change(screen.getByLabelText("US address or landmark"), { target: { value: "Riverside City Hall" } });
    fireEvent.click(screen.getByRole("button", { name: "Find address" }));
    await screen.findByRole("button", { name: "Confirm location and search" });
  });

  it("retains places when detailed routing fails", async () => {
    const original = post.getMockImplementation()!;
    post.mockImplementation(async (...args) => { if (args[0] === "routes") throw new Error("Provider timeout"); return original(...args); });
    start();
    fireEvent.click(await screen.findByRole("button", { name: "Confirm location and search" }));
    await screen.findByText(/Route unavailable: Provider timeout/);
    expect(screen.getByRole("button", { name: "Route to Test Mall" })).toBeEnabled();
  });

  it("retains the confirmed origin and filters for retry after a place-provider outage", async () => {
    const original = post.getMockImplementation()!;
    let failed = false;
    post.mockImplementation(async (...args) => {
      if (args[0] === "search" && !failed) { failed = true; throw new Error("Nearby-place data service unavailable"); }
      return original(...args);
    });
    start();
    fireEvent.click(await screen.findByRole("button", { name: "Confirm location and search" }));
    await screen.findByText("Nearby-place data service unavailable");
    expect(screen.getByRole("checkbox", { name: "Shopping malls" })).toBeChecked();
    expect(screen.getByText("Confirmed starting point")).toBeInTheDocument();
    const retry = screen.getByRole("button", { name: "Retry search" });
    expect(retry).toBeEnabled();
    fireEvent.click(retry);
    await screen.findByText("Route to Test Mall");
    expect(locate).toHaveBeenCalledOnce();
  });

  it("searches automatically on confirmation and only offers an update after filters change", async () => {
    start();
    fireEvent.click(await screen.findByRole("button", { name: "Confirm location and search" }));
    await screen.findByText("Route to Test Mall");
    expect(screen.queryByRole("button", { name: "Update results" })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Resident travel time"), { target: { value: "20" } });
    expect(post.mock.calls.filter(([path]) => path === "search")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Update results" }));
    await screen.findByText("Route to Test Mall");
    expect(post.mock.calls.filter(([path]) => path === "search")).toHaveLength(2);
    expect(post.mock.calls.filter(([path]) => path === "search")[1][1]).toMatchObject({ preferences: { threshold_minutes: 20 } });
  });

  it("retains the confirmed origin for a filter-only follow-up", async () => {
    const view = start();
    fireEvent.click(await screen.findByRole("button", { name: "Confirm location and search" }));
    await screen.findByText("Route to Test Mall");
    view.rerender(<ResidentWorkspace request={{ id: "input-2", prompt: "Make that cycling" }} cancelVersion={0} onBusy={view.onBusy} />);
    await waitFor(() => expect(post.mock.calls.filter(([path]) => path === "search")).toHaveLength(2));
    expect(locate).toHaveBeenCalledOnce();
    const parses = post.mock.calls.filter(([path]) => path === "parse");
    expect(parses[1][1]).toMatchObject({ previous: { categories: ["mall"], threshold_minutes: 10 } });
  });

  it("ignores an old address response after a new address search", async () => {
    let finishOld: (value: unknown) => void = () => {};
    const old = new Promise((resolve) => { finishOld = resolve; });
    post.mockImplementation(async (path, body) => {
      if (path === "origins" && (body as { query: string }).query === "Old address") return old;
      return { candidates: [{ ...origin, label: "New LA address" }], message: "Confirm" };
    });
    render(<ResidentWorkspace request={null} cancelVersion={0} onBusy={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("US address or landmark"), { target: { value: "Old address" } });
    fireEvent.click(screen.getByRole("button", { name: "Find address" }));
    fireEvent.change(screen.getByLabelText("US address or landmark"), { target: { value: "New address" } });
    fireEvent.click(screen.getByRole("button", { name: "Find address" }));
    await screen.findByRole("button", { name: "Confirm location and search" });
    await act(async () => finishOld({ candidates: [{ ...origin, label: "Old Riverside address" }] }));
    expect(screen.queryByText(/Old Riverside address/)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Test map")).toHaveTextContent("New LA address");
  });
  it("resolves a prepared address despite a notice and requires explicit resolution of a genuine blocker", async () => {
    start({ id: "prepared", prompt: "Use transit to a mall", preferences: {
      ...prefs, origin_query: "Riverside Convention Center", needs_clarification: true,
      clarification: "Transit is unavailable. Choose a supported travel mode.",
    } });
    fireEvent.click(await screen.findByRole("button", { name: "Confirm location and search" }));
    expect(post.mock.calls.some(([path]) => path === "parse" || path === "search")).toBe(false);
    expect(screen.getByRole("button", { name: "Update results" })).toBeDisabled();
    expect(screen.getByText("Transit is unavailable. Choose a supported travel mode.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "Public libraries" }));
    expect(screen.getByRole("button", { name: "Update results" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Use the displayed supported filters" }));
    fireEvent.click(screen.getByRole("button", { name: "Update results" }));
    await screen.findByText("Route to Test Mall");
  });

  it("previews imagery for the selected place and keeps navigation usable if the image fails", async () => {
    vi.mocked(getStreetView).mockResolvedValueOnce({ available: true, status: "available", provider: "Mapillary",
      message: "Nearby image", image_url: "https://example.test/street.jpg", viewer_url: "https://www.mapillary.com/app/", distance_m: 25 });
    start();
    fireEvent.click(await screen.findByRole("button", { name: "Confirm location and search" }));
    const photo = await screen.findByAltText("Street imagery near Test Mall");
    expect(photo).toHaveAttribute("src", "https://example.test/street.jpg");
    expect(screen.getByText(/not a verified facility photo/)).toBeInTheDocument();
    fireEvent.error(photo);
    await screen.findByText(/Street imagery could not be loaded/);
    expect(screen.getByRole("button", { name: "Route to Test Mall" })).toBeEnabled();
  });

});


it("ignores an old route response when a different candidate is selected", async () => {
  const expanded: SearchResult = { ...result, candidates: [1, 2, 3, 4].map((id) => ({
    ...result.candidates[0], id: `way/${id}`, name: `Mall ${id}`, duration_minutes: id + 3,
  })) };
  const makeRoute = (id: number): ResidentRoute => ({ ...route, summary: { ...route.summary,
    destination_id: `way/${id}`, destination_name: `Mall ${id}`, green_score: id } });
  let finishOld!: (route: ResidentRoute) => void;
  let finishNew!: (route: ResidentRoute) => void;
  const old = new Promise<ResidentRoute>((resolve) => { finishOld = resolve; });
  const next = new Promise<ResidentRoute>((resolve) => { finishNew = resolve; });
  const original = post.getMockImplementation()!;
  post.mockImplementation(async (...args) => {
    if (args[0] === "search") return expanded;
    if (args[0] === "routes") {
      const id = (args[1] as { candidate_id: string }).candidate_id;
      if (id === "way/2") return old;
      if (id === "way/4") return next;
      return makeRoute(1);
    }
    return original(...args);
  });
  start();
  fireEvent.click(await screen.findByRole("button", { name: "Confirm location and search" }));
  await screen.findByText("Route to Mall 1");
  await waitFor(() => expect(post.mock.calls.some(([path, body]) => path === "routes" && (body as { candidate_id: string }).candidate_id === "way/2")).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: "4. Mall 4" }));
  expect(screen.queryByText("Route to Mall 1")).not.toBeInTheDocument();
  expect(screen.getByLabelText("Test map")).toHaveAttribute("data-routes", "");
  await act(async () => finishOld(makeRoute(2)));
  expect(screen.queryByText("Route to Mall 2")).not.toBeInTheDocument();
  await act(async () => finishNew(makeRoute(4)));
  await screen.findByText("Route to Mall 4");
  expect(screen.getByLabelText("Test map")).toHaveAttribute("data-routes", "selected:way/4");
  expect(post.mock.calls.filter(([path]) => path === "search")).toHaveLength(1);
  expect(post.mock.calls.some(([path, body]) => path === "routes" && (body as { candidate_id: string }).candidate_id === "way/3")).toBe(false);
});


it("waits for server cancellation before searching with changed filters", async () => {
  const original = post.getMockImplementation()!;
  let acknowledge!: (value: unknown) => void;
  const acknowledgement = new Promise((resolve) => { acknowledge = resolve; });
  let searches = 0;
  post.mockImplementation(async (...args) => {
    if (args[0] === "search" && ++searches === 1) {
      return new Promise((_resolve, reject) => args[2]?.addEventListener("abort", () => reject(new DOMException("Cancelled", "AbortError")), { once: true }));
    }
    if (args[0].startsWith("cancel/")) return acknowledgement;
    return original(...args);
  });
  start();
  fireEvent.click(await screen.findByRole("button", { name: "Confirm location and search" }));
  await waitFor(() => expect(searches).toBe(1));
  fireEvent.change(screen.getByLabelText("Resident travel time"), { target: { value: "20" } });
  fireEvent.click(screen.getByRole("button", { name: "Update results" }));
  await waitFor(() => expect(post.mock.calls.some(([path]) => path.startsWith("cancel/"))).toBe(true));
  expect(searches).toBe(1);
  await act(async () => acknowledge({ cancelled: true }));
  await screen.findByText("Route to Test Mall");
  expect(searches).toBe(2);
});

it("keeps failed cancellation retryable and blocks new work until acknowledgement", async () => {
  const original = post.getMockImplementation()!;
  let searches = 0;
  let cancellations = 0;
  post.mockImplementation(async (...args) => {
    if (args[0] === "search" && ++searches === 1) {
      return new Promise((_resolve, reject) => args[2]?.addEventListener("abort", () => reject(new DOMException("Cancelled", "AbortError")), { once: true }));
    }
    if (args[0].startsWith("cancel/") && ++cancellations <= 2) throw new Error("Temporary connection loss");
    return original(...args);
  });
  start();
  fireEvent.click(await screen.findByRole("button", { name: "Confirm location and search" }));
  await waitFor(() => expect(searches).toBe(1));
  fireEvent.click(screen.getByRole("button", { name: "Stop and reset" }));
  await screen.findByText("Could not confirm that the request stopped. Select Stop and reset to retry.");
  fireEvent.click(screen.getByRole("button", { name: "Retry search" }));
  await waitFor(() => expect(cancellations).toBe(2));
  expect(searches).toBe(1);
  fireEvent.click(screen.getByRole("button", { name: "Retry search" }));
  await screen.findByText("Route to Test Mall");
  expect(cancellations).toBe(3);
  expect(searches).toBe(2);
  const cancelIds = post.mock.calls.filter(([path]) => path.startsWith("cancel/")).map(([path]) => path);
  expect(new Set(cancelIds).size).toBe(1);
});


it("uses a named origin even when parsing also suggests browser location", async () => {
  start({ id: "named-la", prompt: "I'm in LA", preferences: { ...prefs, origin_query: "LA", use_current_location: true } });
  await screen.findByRole("button", { name: "Confirm location and search" });
  expect(locate).not.toHaveBeenCalled();
  expect(post.mock.calls.some(([path, body]) => path === "origins" && (body as { query?: string }).query === "LA")).toBe(true);
  expect(screen.queryByRole("checkbox", { name: "Indoors only" })).not.toBeInTheDocument();
});
