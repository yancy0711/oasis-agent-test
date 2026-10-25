"""Offline resident discovery checks; no optimization, providers or LLM calls."""
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic
import json
import pytest
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from oasis.contracts.resident import ResidentOrigin, ResidentPreferences, ResidentSearchRequest
from oasis.core.cancellation import CancellationToken, RunCancelled
from oasis.core.data import overpass
from oasis.core.data.resident_places import normalize_places, fetch_nearby_places
from oasis.services import resident as module
from oasis.services.resident import ResidentService
from api.resident import create_resident_router

ORIGIN = ResidentOrigin(longitude=-117.375, latitude=33.981, label="Riverside, California, United States", country_code="us")


def provider_response(payload, *, status=200, headers=None):
    return httpx.Response(status, json=payload, headers=headers,
                          request=httpx.Request("POST", "https://example.test/api/interpreter"))


def element(identity, tags, **extra):
    return {"type": "node", "id": identity, "lon": -117.374, "lat": 33.981, "tags": tags, **extra}


def place(identity):
    return {"id": str(identity), "name": f"Library {identity}", "category": "library", "longitude": -117.374,
            "latitude": 33.981, "coordinate_source": "Mapped facility point", "osm_url": "https://www.openstreetmap.org/node/1"}


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(module, "coordinate_origin", lambda *args, **kwargs: ORIGIN)
    return ResidentService({"accessibility": {"ors_request_interval_s": 0}})


def search_request(**kwargs):
    return ResidentSearchRequest(request_id="request-123", longitude=ORIGIN.longitude, latitude=ORIGIN.latitude,
        preferences=ResidentPreferences(threshold_minutes=10, **kwargs))


def supply(monkeypatch, places):
    monkeypatch.setattr(module, "fetch_nearby_places", lambda *args, **kwargs: (places, True))


def test_categories_negation_access_and_indoor_evidence():
    rows = [element(1, {"amenity": "library"}), element(2, {"amenity": "community_centre"}),
            element(3, {"leisure": "swimming_pool", "access": "yes", "fee": "yes"}),
            element(4, {"shop": "mall", "indoor": "yes"}),
            element(5, {"leisure": "park"}), element(6, {"leisure": "swimming_pool", "access": "private"})]
    prefs = ResidentPreferences(categories=["library", "community_centre", "swimming_pool", "mall", "park"], excluded_categories=["park"])
    result = normalize_places(rows, prefs, [-117.375, 33.981], 5)
    assert {r["category"] for r in result} == {"library", "community_centre", "swimming_pool", "mall"}
    assert next(r for r in result if r["category"] == "swimming_pool")["fee"] == "yes"
    assert [r["id"] for r in normalize_places(rows, prefs.model_copy(update={"indoor_only": True}), [-117.375, 33.981], 5)] == ["node/4"]
    assert normalize_places(rows, prefs, [0, 0], 5) == []


def test_mapped_public_entrance_is_used_without_inventing_an_entrance():
    rows = [{"type": "way", "id": 1, "center": {"lon": -117.374, "lat": 33.981}, "nodes": [2, 3], "tags": {"shop": "mall"}},
            element(2, {"entrance": "main", "access": "private"}),
            element(3, {"entrance": "yes"}, lon=-117.373)]
    result = normalize_places(rows, ResidentPreferences(), [-117.375, 33.981], 5)
    assert len(result) == 1
    assert result[0]["longitude"] == -117.373
    assert result[0]["coordinate_source"] == "Mapped facility entrance"


def test_osm_query_uses_distinct_tags_and_does_not_cache_partial_response(monkeypatch, tmp_path):
    import oasis.core.data.resident_places as data
    monkeypatch.setattr(data, "CACHE_DIR", tmp_path)
    queries = []
    monkeypatch.setenv("RESIDENT_OVERPASS_URL", "https://example.test/api/interpreter")
    def post(method, url, data, headers, timeout, check):
        assert method == "POST"
        assert url == "https://example.test/api/interpreter"
        assert headers["User-Agent"] == "OASIS-Agent/1.0"
        assert timeout == (5, 45)  # Queue time is separate from the 12s execution limit.
        queries.append(data["data"])
        return provider_response({"elements": [], "remark": "timeout"})
    monkeypatch.setattr(overpass, "cancellable_request", post)
    with pytest.raises(overpass.OverpassUnavailable, match="incomplete result"):
        fetch_nearby_places(-117.375, 33.981, 5, ResidentPreferences())
    assert "[maxsize:134217728]" in queries[0]
    assert '["shop"="mall"]' in queries[0] and '["leisure"="swimming_pool"]' in queries[0]
    assert not list(tmp_path.rglob("*.json"))


def test_network_time_not_straight_line_shortlist_or_budget(service, monkeypatch):
    supply(monkeypatch, [place(i) for i in range(20)])
    seen = []
    def matrix(url, payload, check):
        check(); seen.append(payload)
        return {"durations": [[900] * 18 + [600, 60]]}
    monkeypatch.setattr(service, "_cached_post", matrix)
    result = service.search(search_request())
    assert [r.id for r in result.candidates] == ["19", "18"]
    assert result.candidates[1].duration_minutes == 10
    assert result.checked_count == 20 and result.complete
    assert len(seen[0]["sources"]) == 1 and len(seen[0]["destinations"]) == 20


def test_null_network_times_are_not_assumed_reachable(service, monkeypatch):
    supply(monkeypatch, [place(1), place(2)])
    monkeypatch.setattr(service, "_cached_post", lambda *args: {"durations": [[None, 601]]})
    result = service.search(search_request())
    assert result.complete and result.candidates == []


def test_candidate_work_limit_is_explicit_partial_result(service, monkeypatch):
    supply(monkeypatch, [place(i) for i in range(251)])
    calls = []
    def matrix(url, body, check):
        calls.append(body)
        return {"durations": [[60] * len(body["destinations"])]}
    monkeypatch.setattr(service, "_cached_post", matrix)
    result = service.search(search_request())
    assert not result.complete and result.checked_count == 50 and result.discovered_count == 251
    assert result.candidate_limit == 50 and len(result.candidates) == 50
    assert len(calls) == 1 and len(calls[0]["destinations"]) == 50
    assert "Partial results" in result.message


def test_matrix_timeout_is_not_reported_as_a_complete_empty_search(service, monkeypatch):
    supply(monkeypatch, [place(i) for i in range(51)])
    def matrix(*args):
        raise httpx.ReadTimeout("provider unavailable")
    monkeypatch.setattr(service, "_cached_post", matrix)
    result = service.search(search_request())
    assert not result.complete and result.checked_count == 0 and result.candidates == []
    assert "Partial results" in result.message and "ReadTimeout" in result.message


def test_cancel_before_start_prevents_external_work(service, monkeypatch):
    monkeypatch.setattr(module, "coordinate_origin", lambda *args, **kwargs: pytest.fail("cancelled request did external work"))
    service.cancel("request-123")
    with pytest.raises(RunCancelled): service.search(search_request())


def test_cancel_during_matrix_acknowledges_cleanup_and_allows_new_search(service, monkeypatch):
    supply(monkeypatch, [place(1)])
    started, released = Event(), Event()
    def matrix(url, payload, check):
        started.set()
        deadline = monotonic() + 3
        try:
            while monotonic() < deadline:
                check()
                Event().wait(0.01)
            pytest.fail("The running request did not receive cancellation")
        finally:
            released.set()
    monkeypatch.setattr(service, "_cached_post", matrix)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(service.search, search_request())
        assert started.wait(2)
        assert service.cancel("request-123") is True
        assert released.is_set() and not service.running
        with pytest.raises(RunCancelled):
            pending.result(timeout=1)
    assert not service.searches  # Cancelled work is not a partial result.
    monkeypatch.setattr(service, "_cached_post", lambda *args: {"durations": [[60]]})
    fresh = search_request().model_copy(update={"request_id": "request-next"})
    assert service.search(fresh).complete


def test_non_us_origin_never_downloads_candidates(service, monkeypatch):
    monkeypatch.setattr(module, "coordinate_origin", lambda *args, **kwargs: ORIGIN.model_copy(update={"country_code": "hk", "label": "Hong Kong"}))
    monkeypatch.setattr(module, "fetch_nearby_places", lambda *args, **kwargs: pytest.fail("out-of-area search executed"))
    with pytest.raises(ValueError, match="outside"): service.search(search_request())


def test_route_is_bound_to_search_and_reports_threshold_change(service, monkeypatch):
    supply(monkeypatch, [place(1)])
    monkeypatch.setattr(service, "_cached_post", lambda *args: {"durations": [[60]]})
    result = service.search(search_request(mode="cycling"))
    seen = []
    def directions(url, payload, check):
        seen.append(url)
        return {"features": [{"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-117.375, 33.981], [-117.374, 33.981]]},
                             "properties": {"summary": {"duration": 601, "distance": 1000}}}]}
    monkeypatch.setattr(service, "_cached_post", directions)
    route = service.route(result.search_id, "1")
    assert "cycling-regular" in seen[0]
    assert route["summary"]["within_threshold"] is False
    assert route["summary"]["heat_status"] == "unavailable"
    with pytest.raises(ValueError): service.route(result.search_id, "invented-id")
    with pytest.raises(KeyError): service.route("unknown-search", "1")


def test_cache_distinguishes_mode_origin_and_destination(service, monkeypatch, tmp_path):
    monkeypatch.setattr(module, "CACHE_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(service, "_post", lambda url, payload, check: calls.append((url, payload)) or {"durations": [[60]]})
    body = {"locations": [[0, 0], [1, 1]]}
    for _ in range(2): service._cached_post("matrix/foot-walking", body, lambda: None)
    service._cached_post("matrix/cycling-regular", body, lambda: None)
    service._cached_post("matrix/foot-walking", {"locations": [[0, 1], [1, 1]]}, lambda: None)
    assert len(calls) == 3


def test_parse_uses_resident_schema_and_followup_context_without_solver():
    calls = []
    class LLM:
        def structured(self, system, user, schema):
            calls.append((system, json.loads(user), schema))
            return {"categories": ["mall"], "excluded_categories": ["park"], "mode": "cycling", "threshold_minutes": 10}
    service = SimpleNamespace(cfg={}, _llm_session_config=lambda _: None, llm_factory=lambda: LLM())
    app = FastAPI(); app.include_router(create_resident_router(service))
    client = TestClient(app)
    response = client.post("/v1/resident/parse", json={"prompt": "Only shopping malls, no parks", "previous": {"mode": "cycling", "threshold_minutes": 10}})
    assert response.status_code == 200
    assert response.json()["categories"] == ["mall"] and response.json()["excluded_categories"] == ["park"]
    assert calls[0][1]["previous"]["threshold_minutes"] == 10
    assert "n_facilities" not in calls[0][2]["properties"]
    assert "budget" not in response.json()


@pytest.mark.parametrize("endpoint,outer", [("/v1/resident/parse", False), ("/v1/requests/interpret", True)])
def test_both_parse_paths_use_the_same_configured_semantic_catalog(monkeypatch, endpoint, outer):
    import api.resident as resident_api
    from oasis.settings import load_resident_categories
    catalog = load_resident_categories()
    catalog["library"]["purpose"] = "Configurable catalog description for a book destination."
    monkeypatch.setattr(resident_api, "load_resident_categories", lambda: catalog)
    class LLM:
        def structured(self, system, user, schema):
            assert catalog["library"]["purpose"] in system
            assert "not an exhaustive list" in system
            assert "10 minutes' dirve" in system
            preferences = {"categories": ["library"]}
            return {"action": "find_destination", "workflow": "personal", "reason": "Find a reading destination.",
                    "preferences": preferences} if outer else preferences
    service = SimpleNamespace(cfg={}, _llm_session_config=lambda _: None, llm_factory=lambda: LLM())
    app = FastAPI(); app.include_router(create_resident_router(service))
    response = TestClient(app).post(endpoint, json={"prompt": "Find somewhere for reading"})
    assert response.status_code == 200
    preferences = response.json()["preferences"] if outer else response.json()
    assert preferences["categories"] == ["library"]


def test_category_catalog_covers_exactly_the_discovery_adapters():
    from oasis.settings import load_resident_categories
    from oasis.core.data.resident_places import CATEGORY_SELECTORS
    catalog = load_resident_categories()
    assert set(catalog) == set(CATEGORY_SELECTORS)
    assert all(row.get("purpose") and row.get("activities") and row.get("limits") for row in catalog.values())


def test_hosted_endpoint_key_is_not_copied_into_cached_metadata(monkeypatch):
    endpoint = "https://managed.example/v1/private-test-key/interpreter"
    monkeypatch.setattr(overpass, "cancellable_request", lambda *a, **k: provider_response({"elements": []}))
    payload = overpass._fetch_payload([endpoint], "test query", "hosted-key-test", 45, lambda: None)
    assert payload["_oasis_endpoint"] == "managed.example"
    assert "private-test-key" not in json.dumps(payload)


def test_hosted_header_key_is_sent_only_to_its_provider(monkeypatch):
    monkeypatch.setenv("OVERPASS_CH_API_KEY", "test-header-secret")
    calls = []
    def request(method, url, **kwargs):
        calls.append((url, kwargs["headers"]))
        return provider_response({"elements": []})
    monkeypatch.setattr(overpass, "cancellable_request", request)
    hosted = "https://api.overpass.ch/v1/interpreter"
    other = "https://public.example/api/interpreter"
    payload = overpass._fetch_payload([hosted], "test", "hosted-header", 45, lambda: None)
    overpass._fetch_payload([other], "test", "other-header", 45, lambda: None)
    assert calls[0][1]["X-API-Key"] == "test-header-secret"
    assert "X-API-Key" not in calls[1][1]
    assert "test-header-secret" not in json.dumps(payload)


def test_origin_api_keeps_outside_coordinates_for_map_warning(monkeypatch):
    monkeypatch.setattr(module, "coordinate_origin", lambda *args, **kwargs: ResidentOrigin(longitude=114.17, latitude=22.3, label="Hong Kong", country_code="hk"))
    app = FastAPI(); app.include_router(create_resident_router(SimpleNamespace(cfg={})))
    client = TestClient(app)
    response = client.post("/v1/resident/origin", json={"longitude": 114.17, "latitude": 22.3})
    assert response.status_code == 200 and response.json()["country_code"] == "hk"
    assert response.json()["longitude"] == 114.17
    assert client.post("/v1/resident/origin", json={"longitude": 200, "latitude": 22.3}).status_code == 422


def test_place_provider_outage_returns_retryable_api_error(monkeypatch, tmp_path):
    import oasis.core.data.resident_places as data
    monkeypatch.setattr(data, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(module, "coordinate_origin", lambda *args, **kwargs: ORIGIN)
    def unavailable(*args, **kwargs):
        provider_response({}, status=500).raise_for_status()
    monkeypatch.setattr(overpass, "cancellable_request", unavailable)
    app = FastAPI(); app.include_router(create_resident_router(SimpleNamespace(cfg={})))
    response = TestClient(app).post("/v1/resident/search", json=search_request().model_dump())
    assert response.status_code == 503
    assert "Your inputs are kept" in response.json()["detail"]
    assert not list(tmp_path.rglob("*.json"))


def test_notice_does_not_block_search_but_explicit_clarification_does(service, monkeypatch):
    supply(monkeypatch, [])
    request = search_request()
    request.preferences.clarification = "Potential cooling places are not guaranteed official centers."
    assert service.search(request).complete
    request.preferences.needs_clarification = True
    with pytest.raises(ValueError, match="Potential cooling"):
        service.search(request)


@pytest.mark.parametrize("workflow,audience", [("personal", "research"), ("research", "personal"), ("clarify", "research")])
def test_intent_contract_is_independent_of_selected_tab(workflow, audience):
    class LLM:
        def structured(self, system, user, schema):
            assert json.loads(user)["current_tab"] == audience
            assert "workflow" in schema["properties"]
            return {"action": {"personal": "find_destination", "research": "select_facilities", "clarify": "clarify"}[workflow],
                    "workflow": workflow, "reason": "Intent determines the workflow", "preferences": {} if workflow == "personal" else None}
    app = FastAPI()
    app.include_router(create_resident_router(SimpleNamespace(cfg={}, _llm_session_config=lambda _: None, llm_factory=lambda: LLM())))
    response = TestClient(app).post("/v1/requests/interpret", json={"prompt": "test request", "audience": audience})
    assert response.status_code == 200
    assert response.json()["workflow"] == workflow


def test_action_controls_workflow_and_default_categories_are_all_selected():
    from oasis.contracts.resident import RequestInterpretation
    selection = RequestInterpretation(action="select_facilities", workflow="personal", reason="A landmark was mentioned.", preferences={})
    assert selection.workflow == "research" and selection.preferences is None
    destination = RequestInterpretation(action="find_destination", workflow="research", reason="Nearby place.", preferences={})
    assert destination.workflow == "personal"
    assert set(destination.preferences.categories) == {"library", "community_centre", "swimming_pool", "mall",
        "sports_centre", "arts_centre", "place_of_worship", "park"}


def test_overpass_fallback_is_bounded_and_success_is_cached(monkeypatch, tmp_path):
    import oasis.core.data.resident_places as data
    monkeypatch.setattr(data, "CACHE_DIR", tmp_path)
    monkeypatch.delenv("RESIDENT_OVERPASS_URL", raising=False)
    calls = []
    def post(method, url, **kwargs):
        calls.append(url)
        if len(calls) == 1: raise httpx.ReadTimeout("primary down")
        return provider_response({"elements": [element(1, {"shop": "mall"})]})
    monkeypatch.setattr(overpass, "cancellable_request", post)
    for _ in range(2):
        rows, complete = fetch_nearby_places(-117.375, 33.981, 5, ResidentPreferences())
        assert complete and len(rows) == 1
    assert len(calls) == 2 and calls[0] != calls[1]


def test_results_are_nearest_first_even_with_a_category_preference(service, monkeypatch):
    supply(monkeypatch, [place(1), {**place(2), "category": "mall"}])
    monkeypatch.setattr(service, "_cached_post", lambda *args: {"durations": [[60, 120]]})
    result = service.search(search_request(preferred_categories=["mall"]))
    assert [p.id for p in result.candidates] == ["1", "2"]


@pytest.fixture(autouse=True)
def reset_overpass_cooldown(monkeypatch):
    monkeypatch.setattr(overpass, "_FAILURES", __import__("collections").OrderedDict())
    monkeypatch.setattr(overpass, "_RATE_LIMITS", {})
    monkeypatch.delenv("OASIS_OVERPASS_URL", raising=False)
    monkeypatch.delenv("RESIDENT_OVERPASS_URL", raising=False)
    monkeypatch.delenv("OASIS_OSM_USER_AGENT", raising=False)


def test_overpass_429_reads_slot_status_and_stops_fallback(monkeypatch, tmp_path):
    import oasis.core.data.resident_places as data
    monkeypatch.setattr(data, "CACHE_DIR", tmp_path)
    monkeypatch.delenv("RESIDENT_OVERPASS_URL", raising=False)
    now = [100.]
    monkeypatch.setattr(data.time, "monotonic", lambda: now[0])
    calls, status_calls = [], []
    def request(method, url, **kwargs):
        if method == "GET":
            status_calls.append(url)
            return httpx.Response(200, text="Slot available after: date, in 60 seconds.",
                                  request=httpx.Request("GET", url))
        calls.append(url)
        provider_response({}, status=429, headers={"Retry-After": "40"}).raise_for_status()
    monkeypatch.setattr(overpass, "cancellable_request", request)
    with pytest.raises(RuntimeError, match="Retry in 62 seconds"):
        fetch_nearby_places(-117.375, 33.981, 5, ResidentPreferences())
    assert len(calls) == 1
    assert status_calls == ["https://overpass-api.de/api/status"]


def test_preexisting_cooldown_skips_only_that_provider(monkeypatch):
    first, second = "https://first.test/api/interpreter", "https://second.test/api/interpreter"
    monkeypatch.setitem(overpass._RATE_LIMITS, first, monotonic() + 60)
    calls = []
    def request(method, url, **kwargs):
        calls.append(url)
        return provider_response({"elements": []})
    monkeypatch.setattr(overpass, "cancellable_request", request)
    assert overpass._fetch_payload([first, second], "query", "cooldown-key", 45, lambda: None)["elements"] == []
    assert calls == [second]


def test_all_providers_cooling_down_make_no_request_or_query_failure(monkeypatch):
    for endpoint in overpass.DEFAULT_ENDPOINTS:
        monkeypatch.setitem(overpass._RATE_LIMITS, endpoint, monotonic() + 60)
    monkeypatch.setattr(overpass, "cancellable_request", lambda *args, **kwargs: pytest.fail("Cooling provider contacted"))
    with pytest.raises(overpass.OverpassUnavailable, match="providers are cooling down"):
        overpass._fetch_payload(overpass.DEFAULT_ENDPOINTS, "query", "all-cooling", 45, lambda: None)
    assert "all-cooling" not in overpass._FAILURES


def test_partial_http200_tries_fallback_and_caches_only_complete_results(monkeypatch, tmp_path):
    from oasis.core.data import resident_places as data
    monkeypatch.setattr(data, "CACHE_DIR", tmp_path)
    calls = []
    def request(method, url, **kwargs):
        calls.append(url)
        remark = {"remark": 'runtime error: Query timed out in "recurse" at line 1'} if len(calls) == 1 else {}
        return provider_response({"elements": [element(1, {"amenity": "library"})], **remark})
    monkeypatch.setattr(overpass, "cancellable_request", request)
    for _ in range(2):
        places, complete = data.fetch_nearby_places(-117.375, 33.981, 5, ResidentPreferences())
        assert complete and len(places) == 1
    assert calls == list(reversed(overpass.DEFAULT_ENDPOINTS))
    assert all("remark" not in json.loads(path.read_text()) for path in tmp_path.rglob("*.json"))


def test_benign_remark_is_not_a_query_failure(monkeypatch, tmp_path):
    from oasis.core.data import resident_places as data
    monkeypatch.setattr(data, "CACHE_DIR", tmp_path)
    calls = []
    def request(method, url, **kwargs):
        calls.append(url)
        return provider_response({"elements": [], "remark": "Informational note"})
    monkeypatch.setattr(overpass, "cancellable_request", request)
    assert data.fetch_nearby_places(-117.375, 33.981, 5, ResidentPreferences()) == ([], True)
    assert data.fetch_nearby_places(-117.375, 33.981, 5, ResidentPreferences()) == ([], True)
    assert len(calls) == 1


def test_failure_blocks_only_same_query_for_five_seconds(monkeypatch, tmp_path):
    import oasis.core.data.resident_places as data
    monkeypatch.setattr(data, "CACHE_DIR", tmp_path)
    now, calls = [100.], []
    monkeypatch.setattr(overpass.time, "monotonic", lambda: now[0])
    def post(method, url, **kwargs):
        calls.append(url)
        raise httpx.ReadTimeout("stalled provider")
    monkeypatch.setattr(overpass, "cancellable_request", post)
    for _ in range(4):
        with pytest.raises(RuntimeError, match="lookup failed"):
            fetch_nearby_places(-117.375, 33.981, 5, ResidentPreferences())
        with pytest.raises(RuntimeError, match="Retry in 5 seconds"):
            fetch_nearby_places(-117.375, 33.981, 5, ResidentPreferences())
        now[0] += 5
    assert len(calls) == 8  # Still retryable after three failures; no 5-minute pause.
    with pytest.raises(RuntimeError, match="lookup failed"):
        fetch_nearby_places(-117.374, 33.981, 5, ResidentPreferences())
    assert len(calls) == 10  # A new location can be tried immediately.


def test_overpass_cache_remains_available_during_provider_cooldown(monkeypatch, tmp_path):
    import oasis.core.data.resident_places as data
    monkeypatch.setattr(data, "CACHE_DIR", tmp_path)
    calls = []
    def post(method, url, **kwargs):
        calls.append(url)
        return provider_response({"elements": []})
    monkeypatch.setattr(overpass, "cancellable_request", post)
    fetch_nearby_places(-117.375, 33.981, 5, ResidentPreferences())
    monkeypatch.setattr(overpass, "_RATE_LIMITS", {overpass.DEFAULT_ENDPOINTS[0]: data.time.monotonic() + 60})
    assert fetch_nearby_places(-117.375, 33.981, 5, ResidentPreferences()) == ([], True)
    assert len(calls) == 1


def test_overpass_does_not_issue_parallel_resident_queries(monkeypatch, tmp_path):
    import oasis.core.data.resident_places as data
    monkeypatch.setattr(data, "CACHE_DIR", tmp_path)
    with overpass._QUERY_LOCK:
        with pytest.raises(RuntimeError, match="lookup is finishing"):
            fetch_nearby_places(-117.375, 33.981, 5, ResidentPreferences())


def test_nearby_origins_share_complete_region_but_filter_actual_radius(monkeypatch, tmp_path):
    import oasis.core.data.resident_places as data
    monkeypatch.setattr(data, "CACHE_DIR", tmp_path)
    calls = []
    def post(method, url, **kwargs):
        calls.append(kwargs["data"]["data"])
        return provider_response({"elements": [
            element(1, {"amenity": "library"}, lon=-117.3655, lat=33.981),
            element(2, {"amenity": "library"}, lon=-117.35, lat=33.981)]})
    monkeypatch.setattr(overpass, "cancellable_request", post)
    prefs = ResidentPreferences(categories=["library"])
    first, complete = fetch_nearby_places(-117.374, 33.981, 1, prefs)
    second, _ = fetch_nearby_places(-117.373, 33.982, 1, prefs)
    assert complete and [p["id"] for p in first] == ["node/1"]
    assert [p["id"] for p in second] == ["node/1"]
    assert len(calls) == 1 and "around:2000" in calls[0]


def test_green_score_uses_distance_and_rejects_missing_or_partial_evidence():
    from oasis.services.resident import route_green_score
    def feature(rows):
        return {"properties": {"summary": {"distance": 1000}, "extras": {"green": {"summary": rows}}}}
    assert route_green_score(feature([{"value": 0, "distance": 750}, {"value": 8, "distance": 250}])) == 2
    assert route_green_score(feature([{"value": 0, "distance": 1000}])) == 0
    assert route_green_score(feature([{"value": 8, "distance": 250}])) is None
    assert route_green_score(feature([{"value": 11, "distance": 1000}])) is None
    assert route_green_score({"properties": {}}) is None


def test_walking_requests_green_evidence_without_another_osm_lookup(service, monkeypatch):
    supply(monkeypatch, [place(1)])
    monkeypatch.setattr(service, "_cached_post", lambda *args: {"durations": [[60]]})
    result = service.search(search_request())
    monkeypatch.setattr(module, "fetch_nearby_places", lambda *args, **kwargs: pytest.fail("Routing must not discover POIs again"))
    def directions(url, body, check):
        assert body["extra_info"] == ["green"]
        return {"features": [{"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-117.375, 33.981], [-117.374, 33.981]]},
            "properties": {"summary": {"duration": 60, "distance": 100}, "extras": {"green": {"summary": [{"value": 7, "distance": 100}]}}}}]}
    monkeypatch.setattr(service, "_cached_post", directions)
    route = service.route(result.search_id, "1")
    assert route["summary"]["green_score"] == 7
    assert route["summary"]["heat_status"] == "unavailable"


def test_cancelled_overpass_releases_owner_lock_without_fallback_or_cooldown(monkeypatch):
    token, calls = CancellationToken(), []
    def request(method, url, **kwargs):
        calls.append(url)
        token.cancel()
        kwargs["check"]()
    monkeypatch.setattr(overpass, "cancellable_request", request)
    with pytest.raises(RunCancelled):
        overpass._fetch_payload(overpass.DEFAULT_ENDPOINTS, "query", "cancelled-key", 45, token.check)
    assert len(calls) == 1  # Cancellation must not try the fallback endpoint.
    assert "cancelled-key" not in overpass._FAILURES
    assert overpass._QUERY_LOCK.acquire(blocking=False)
    overpass._QUERY_LOCK.release()
    monkeypatch.setattr(overpass, "cancellable_request", lambda *args, **kwargs: provider_response({"elements": []}))
    assert overpass._fetch_payload(overpass.DEFAULT_ENDPOINTS, "query", "cancelled-key", 45, lambda: None)["elements"] == []


def test_selected_route_has_cancel_token_and_preserves_candidate_search(service, monkeypatch):
    supply(monkeypatch, [place(1)])
    monkeypatch.setattr(service, "_cached_post", lambda *args: {"durations": [[60]]})
    snapshot = service.search(search_request())
    started = Event()
    def directions(url, payload, check):
        started.set()
        deadline = monotonic() + 3
        while monotonic() < deadline:
            check()
            Event().wait(0.01)
        pytest.fail("Route did not receive cancellation")
    monkeypatch.setattr(service, "_cached_post", directions)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(service.route, snapshot.search_id, "1", "route-request")
        assert started.wait(2)
        assert service.cancel("route-request") is True
        with pytest.raises(RunCancelled):
            pending.result(timeout=1)
    assert not service.running
    assert snapshot.search_id in service.searches


def test_cancel_endpoint_does_not_acknowledge_while_worker_is_still_stopping(monkeypatch):
    monkeypatch.setattr(ResidentService, "cancel", lambda self, request_id: False)
    app = FastAPI()
    app.include_router(create_resident_router(SimpleNamespace(cfg={})))
    response = TestClient(app).post("/v1/resident/cancel/still-stopping")
    assert response.status_code == 503
    assert "still stopping" in response.json()["detail"]


def test_geocoder_retry_exhaustion_retains_provider_diagnostic(monkeypatch):
    from oasis.core.retry import TransientError
    def failed_origin(*args, **kwargs):
        raise TransientError("Nominatim 503")
    monkeypatch.setattr(module, "coordinate_origin", failed_origin)
    app = FastAPI()
    app.include_router(create_resident_router(SimpleNamespace(cfg={})))
    response = TestClient(app).post("/v1/resident/origin", json={
        "longitude": -117.375, "latitude": 33.981, "request_id": "origin-request"})
    assert response.status_code == 503
    assert response.json()["detail"] == "Nominatim 503"


def test_named_origin_filters_non_us_options_and_requests_us_scope(monkeypatch):
    calls = []
    def search(query, **kwargs):
        calls.append((query, kwargs))
        return [
            {"lon": "-118.24", "lat": "34.05", "display_name": "Los Angeles", "address": {"country_code": "us"}},
            {"lon": "2.3", "lat": "48.8", "display_name": "Outside US", "address": {"country_code": "fr"}},
        ]
    monkeypatch.setattr(module, "search_origin_candidates", search)
    origins = module.named_origins("LA")
    assert len(origins) == 1 and origins[0].country_code == "us"
    assert calls[0][1]["countrycodes"] == "us"


def test_resident_parse_does_not_restore_removed_strict_indoor_filter():
    class LLM:
        def structured(self, system, user, schema):
            return {"categories": ["library", "mall"], "indoor_only": True}
    app = FastAPI()
    app.include_router(create_resident_router(SimpleNamespace(cfg={}, _llm_session_config=lambda _: None, llm_factory=lambda: LLM())))
    response = TestClient(app).post("/v1/resident/parse", json={"prompt": "Find indoor places"})
    assert response.status_code == 200
    assert response.json()["categories"] == ["library", "mall"]
    assert response.json()["indoor_only"] is False
