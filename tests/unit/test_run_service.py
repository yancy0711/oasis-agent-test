"""Durable mode, cancellation, and revision tests for the run service."""
from __future__ import annotations

import json
import multiprocessing
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from oasis.agent.context import Context
from oasis.contracts.domain import ParetoPoint, SiteSolution, TaskSpec
from oasis.services.application import OasisService
from oasis.services.run_store import RunStore


class _ParseLLM:
    def __init__(self, payload):
        self.payload = payload

    def structured(self, _system, _user, schema=None):
        assert schema and schema.get("title") == "TaskSpec"
        return self.payload


def _parse_service(payload) -> OasisService:
    service = object.__new__(OasisService)
    service.cfg = {
        "optimization": {"n_facilities": 10},
        "accessibility": {"threshold_minutes": 15},
    }
    service.profiles = {}
    service.llm_factory = lambda: _ParseLLM(payload)
    service.llm_sessions = {}
    service.lock = threading.Lock()
    return service


def test_solver_phase_budgets_use_configured_preview_limit():
    service = _parse_service({})
    service.cfg["optimization"].update(solver_time_limit_s=180, solver_preview_time_limit_s=60)
    assert service._phase_config("quick", Path("outputs_run"))["optimization"]["solver_time_limit_s"] == 60
    assert service._phase_config("precise", Path("outputs_run"))["optimization"]["solver_time_limit_s"] == 180
    service.cfg["optimization"]["solver_time_limit_s"] = 20
    assert service._phase_config("quick", Path("outputs_run"))["optimization"]["solver_time_limit_s"] == 20
    assert service.cfg["optimization"]["solver_preview_time_limit_s"] == 60


@pytest.mark.parametrize("primary", ["minimum_facilities", "minimum_threshold"])
def test_diagnostic_artifacts_publish_both_maps_and_prefer_primary(tmp_path, primary):
    service = _parse_service({})
    diagnostics = {}
    for name in ("minimum_facilities", "minimum_threshold"):
        path = tmp_path / f"{name}.geojson"
        path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
        diagnostics[name] = {"map": str(path)}
    result = service._artifacts("paired", {"task": {"analysis_goal": primary}, "diagnostics": diagnostics}, tmp_path)
    assert result["map"] == result[f"{primary}_map"]
    assert "minimum_facilities_map" in result and "minimum_threshold_map" in result


def test_parse_requires_one_choice_when_the_model_extracts_multiple_places():
    service = _parse_service({
        "place_name": "Phoenix, Arizona, USA",
        "place_names": ["Phoenix, Arizona, USA", "New York, New York, USA"],
        "analysis_goal": "facility_siting",
        "n_facilities": 10,
        "ambiguities": [{
            "field": "place_names",
            "candidates": ["Phoenix, Arizona, USA", "New York, New York, USA"],
            "question": "Which city should this run use?",
        }],
    })

    result = service.parse("Select 10 candidate facilities in Phoenix and NYC.")

    assert result["disposition"] == "needs_clarification"
    assert result["place_name_options"] == ["Phoenix, Arizona, USA", "New York, New York, USA"]
    assert "one study area" in result["message"]


def test_parse_surfaces_typed_non_place_ambiguity_instead_of_guessing():
    service = _parse_service({
        "place_name": "Phoenix, Arizona, USA",
        "place_names": ["Phoenix, Arizona, USA"],
        "analysis_goal": "facility_siting",
        "ambiguities": [{
            "field": "n_facilities", "candidates": ["5", "10"],
            "question": "Should the facility budget be 5 or 10?",
        }],
    })

    result = service.parse("Improve access in Phoenix with five or ten facilities.")

    assert result["disposition"] == "needs_clarification"
    assert "5 or 10" in result["message"]


def test_unresolved_route_origin_preserves_an_existing_study_area(monkeypatch):
    service = _parse_service({
        "place_name": "Phoenix, Arizona, USA",
        "analysis_goal": "facility_siting",
        "route_origin": {
            "entity": "unclear entrance", "context": "Phoenix",
            "positional_hint": "", "raw_text": "unclear entrance", "source": "llm",
        },
    })
    monkeypatch.setattr(
        "oasis.services.application.route_origin_options",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("Origin not found.")),
    )

    result = service.parse("From the unclear entrance in Phoenix, find a facility.")

    assert result["disposition"] == "needs_clarification"
    assert result["missing_place"] is False
    assert result["task"]["place_name"] == "Phoenix, Arizona, USA"
    assert result["task"]["route_origin"] is None
    assert "study area was preserved" in result["reason"]


def test_parse_keeps_supported_work_when_one_result_query_is_unavailable():
    service = _parse_service({
        "place_name": "Phoenix, Arizona, USA",
        "place_names": ["Phoenix, Arizona, USA"],
        "analysis_goal": "tract_review",
        "result_queries": [
            {"metric": "heat_exposure", "limit": 5, "raw_text": "highest heat"},
            {"metric": "crime_rate", "limit": 5, "raw_text": "crime rate"},
        ],
    })

    result = service.parse("Show the five hottest tracts and crime rate in Phoenix.")

    assert result["disposition"] == "quick_diagnostic"
    assert [query["metric"] for query in result["task"]["result_queries"]] == ["heat_exposure"]
    assert "crime rate" in result["reason"]
    assert result["alternative"] == ""


def test_minimum_facility_request_is_presented_as_a_quick_diagnostic():
    service = _parse_service({
        "place_name": "Washington, District of Columbia, USA",
        "analysis_goal": "minimum_facilities",
        "threshold_minutes": 15,
        "coverage_target": 1.0,
    })
    result = service.parse(
        "How few cooling centers does DC actually need to cover everyone in 15 minutes?"
    )
    assert result["disposition"] == "quick_diagnostic"


def test_precise_minimum_diagnostic_keeps_labeled_approximate_threshold_bound():
    task = TaskSpec(
        place_name="Phoenix, Arizona, USA", analysis_goal="minimum_facilities",
        n_facilities=10, threshold_minutes=15, coverage_target=1.0,
        diagnostic_fixed_inputs=["n_facilities", "threshold_minutes"],
    )
    preview = {
        "message": (
            "No number of the retained candidate facilities can cover 100% of the all group "
            "within 15 minutes. With 10 facilities, the smallest computed threshold is "
            "131.6 minutes by walking."
        ),
        "diagnostics": {"minimum_threshold": {
            "feasible": True, "minimum_threshold_minutes": 131.6,
            "n_facilities": 10,
        }},
    }
    ranking = "Top 5 for heat exposure: 1. tract 11001004902 (43.498)."
    preview["message"] += " " + ranking
    precise = {
        "message": "The precise candidate set is infeasible at 15 minutes. " + ranking,
        "diagnostics": {
            "minimum_facilities": {"feasible": False},
            "minimum_threshold": {"feasible": False},
        },
    }

    OasisService._carry_preview_diagnostic_bounds(task, preview, precise)

    bound = precise["diagnostics"]["approximate_minimum_threshold"]
    assert bound["accuracy"] == "approximate_haversine"
    assert bound["minimum_threshold_minutes"] == 131.6
    assert "not an ORS network bound" in precise["message"]
    assert precise["message"].count(ranking) == 1


def test_count_only_diagnostic_retains_a_labeled_approximate_arrangement():
    task = TaskSpec(place_name="Riverside", analysis_goal="minimum_facilities", threshold_minutes=12)
    preview = {"diagnostics": {"coverage_alternative": {
        "feasible": True, "threshold_minutes": 38.7, "minimum_facilities": 12,
        "feasible_facility_count": 12, "selected_ids": ["a", "b"],
    }}}
    precise = {"message": "12 minutes is infeasible.", "diagnostics": {}}
    OasisService._carry_preview_diagnostic_bounds(task, preview, precise)
    alternative = precise["diagnostics"]["coverage_alternative"]
    assert alternative["accuracy"] == "approximate_haversine"
    assert alternative["selected_ids"] == ["a", "b"]
    assert "minimum_threshold" not in precise["diagnostics"]
    assert "38.7 minutes" in precise["message"] and "not been verified by ORS" in precise["message"]


def test_parse_rejects_only_when_every_requested_result_is_unavailable():
    service = _parse_service({
        "place_name": "Phoenix, Arizona, USA",
        "analysis_goal": "tract_review",
        "result_queries": [{"metric": "crime_rate", "raw_text": "crime rate"}],
    })
    assert service.parse("Rank Phoenix tracts by crime rate.")["disposition"] == "out_of_scope"


def test_browser_coordinate_returns_smallest_supported_us_parents_first(monkeypatch):
    service = object.__new__(OasisService)
    monkeypatch.setattr(
        "oasis.services.application.reverse_origin_address",
        lambda *_: {
            "country_code": "us", "city": "New York", "county": "New York County",
            "state": "New York",
        },
    )
    result = service.reverse_route_origin(-73.985, 40.758)
    assert result["outside_us"] is False
    assert [item["place_type"] for item in result["candidates"]] == ["city", "county"]
    assert result["candidates"][0]["place_name"] == "New York, New York, USA"
    assert result["candidates"][0]["longitude"] == -73.985


def test_browser_coordinate_outside_us_returns_no_address_prompt(monkeypatch):
    service = object.__new__(OasisService)
    monkeypatch.setattr(
        "oasis.services.application.reverse_origin_address",
        lambda *_: {"country_code": "hk", "city": "Hong Kong"},
    )
    result = service.reverse_route_origin(114.17, 22.3)
    assert result["outside_us"] is True
    assert result["candidates"] == []
    assert "outside the United States" in result["message"]


def test_manual_origin_search_preserves_route_semantics(monkeypatch):
    service = object.__new__(OasisService)
    monkeypatch.setattr(
        "oasis.services.application.route_origin_options",
        lambda *_args, **_kwargs: ([{
            "candidate_id": "miami", "display_name": "Miami, Florida",
            "place_name": "Miami, Florida, USA", "place_type": "city",
            "entity": "Miami", "context": "Florida", "positional_hint": "",
            "administrative": True, "longitude": -80.19, "latitude": 25.76,
        }], True),
    )
    result = service.search_route_origin("Miami")
    assert result["candidates"][0]["administrative"] is False
    assert result["candidates"][0]["place_name"] == "Miami, Florida, USA"


def test_public_llm_endpoints_are_fixed_or_explicitly_approved(monkeypatch):
    service = object.__new__(OasisService)
    service.cfg = {"llm": {"allowed_custom_hosts": []}}
    assert service._validated_llm_url(
        "gemini", "https://generativelanguage.googleapis.com/v1beta/openai/", False,
    ) == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert service._validated_llm_url("openai", "https://api.openai.com/v1", False) == "https://api.openai.com/v1"
    monkeypatch.setattr("oasis.services.application.socket.getaddrinfo", lambda *_: [(None, None, None, None, ("8.8.8.8", 443))])
    with pytest.raises(ValueError, match="not approved"):
        service._validated_llm_url("openai", "https://attacker.example/v1", False)
    with pytest.raises(ValueError, match="not approved"):
        service._validated_llm_url("custom", "https://models.example/v1", False)
    service.cfg["llm"]["allowed_custom_hosts"] = ["models.example"]
    assert service._validated_llm_url("custom", "https://models.example/v1", False) == "https://models.example/v1"
    assert service._validated_llm_url("openai", "https://models.example/v1", False) == "https://models.example/v1"


def test_remote_visitors_cannot_reach_private_model_endpoints(monkeypatch):
    service = object.__new__(OasisService)
    service.cfg = {"llm": {"allowed_custom_hosts": ["internal.example"]}}
    monkeypatch.setattr("oasis.services.application.socket.getaddrinfo", lambda *_: [(None, None, None, None, ("127.0.0.1", 443))])
    with pytest.raises(ValueError, match="Private or loopback"):
        service._validated_llm_url("custom", "https://internal.example/v1", False)


def test_connection_success_requires_a_completion_from_the_selected_model(monkeypatch):
    seen = {}
    class ProbeClient:
        def __init__(self, config):
            seen.update(config)
        def probe(self, _system, _user, schema=None):
            assert schema and schema.get("title") == "TaskSpec"
            return {
                "place_name": "Phoenix, Arizona, USA",
                "analysis_goal": "facility_siting",
                "n_facilities": 2,
                "threshold_minutes": 15,
            }, "deepseek/deepseek-v4-flash-202608"

    service = object.__new__(OasisService)
    service.cfg = {"llm": {"max_tokens": 1024}}
    service.lock = threading.Lock()
    service.llm_sessions = {}
    service._validated_llm_url = lambda *_: "https://openrouter.ai/api/v1"
    monkeypatch.setattr("oasis.services.application.LLMClient", ProbeClient)

    result = service.test_llm_connection(
        "openrouter", "https://openrouter.ai/api/v1",
        "deepseek/deepseek-v4-flash-latest", "test-key",
    )

    assert seen["model"] == "deepseek/deepseek-v4-flash-latest"
    assert result["resolved_model"] == "deepseek/deepseek-v4-flash-202608"
    assert result["structured_output"] is True
    assert result["compatibility_attempts"] == 1
    stored = service.llm_sessions[result["session_id"]]["config"]
    assert stored["max_tokens"] == 1024


def test_connection_probe_retries_one_semantically_wrong_json_response(monkeypatch):
    """A valid JSON object with wrong task values gets one bounded correction attempt."""
    responses = [
        {
            "place_name": "Phoenix, Arizona, USA", "analysis_goal": "facility_siting",
            "n_facilities": 10, "threshold_minutes": 15, "mobility_scenario": "walking",
        },
        {
            "place_name": "Phoenix, Arizona, USA", "analysis_goal": "facility_siting",
            "n_facilities": "2", "threshold_minutes": 15, "mobility_scenario": "walking",
        },
    ]

    class ProbeClient:
        def __init__(self, _config):
            pass

        def probe(self, _system, _user, schema=None):
            assert schema and schema.get("title") == "TaskSpec"
            return responses.pop(0), "provider/resolved-model"

    service = object.__new__(OasisService)
    service.cfg = {"llm": {"max_tokens": 4096}}
    service.lock = threading.Lock()
    service.llm_sessions = {}
    service._validated_llm_url = lambda *_: "https://openrouter.ai/api/v1"
    monkeypatch.setattr("oasis.services.application.LLMClient", ProbeClient)

    result = service.test_llm_connection(
        "openrouter", "https://openrouter.ai/api/v1", "provider/model", "test-key",
    )

    assert result["compatibility_attempts"] == 2
    assert not responses

def test_connection_probe_retries_one_transient_structured_output_failure(monkeypatch):
    """An empty/text compatibility failure is retried inside one connection test."""
    calls = 0

    class ProbeClient:
        def __init__(self, _config):
            pass

        def probe(self, _system, _user, schema=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ValueError("The selected model returned empty content instead of JSON.")
            return {
                "place_name": "Phoenix, Arizona, USA", "analysis_goal": "facility_siting",
                "n_facilities": 2, "threshold_minutes": 15, "mobility_scenario": "walking",
            }, "provider/resolved-model"

    service = object.__new__(OasisService)
    service.cfg = {"llm": {"max_tokens": 4096}}
    service.lock = threading.Lock()
    service.llm_sessions = {}
    service._validated_llm_url = lambda *_: "https://openrouter.ai/api/v1"
    monkeypatch.setattr("oasis.services.application.LLMClient", ProbeClient)

    result = service.test_llm_connection(
        "openrouter", "https://openrouter.ai/api/v1", "provider/model", "test-key",
    )

    assert calls == 2
    assert result["compatibility_attempts"] == 2

class _RoutingOrchestrator:
    """Return the router declared by the phase configuration."""

    def __init__(self, llm, cfg):
        self.cfg = cfg
        self.ctx = Context()

    def run(self, task):
        router = self.cfg["accessibility"]["router"]
        used = "ors_sparse" if router == "ors" else "haversine"
        report = Path(self.cfg["output"]["dir"]) / "report.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("Test report.", encoding="utf-8")
        return {
            "ok": True,
            "paused": False,
            "message": "done",
            "failed_tool": None,
            "results": [{
                "tool": "build_travel_matrix",
                "ok": True,
                "summary": {"router": used, "tier": "live"},
                "provenance": {"tier": "live"},
            }],
            "lineage": [],
            "outputs": {"report": str(report)},
            "heat_summary": None,
            "diagnostics": {},
            "narrative": None,
            "recovery": None,
            "history": [],
        }


class _FallbackOrchestrator(_RoutingOrchestrator):
    """Simulate an invalid Haversine fallback during a precise phase."""

    def run(self, task):
        payload = super().run(task)
        payload["results"][0]["summary"]["router"] = "haversine"
        return payload


class _ReturnedRoutingFailureOrchestrator(_RoutingOrchestrator):
    """Return the strict-routing guard as a structured agent failure."""

    def run(self, task):
        payload = super().run(task)
        if self.cfg["accessibility"]["router"] == "ors":
            diagnosis = (
                "Precise mode requires ORS for every mobility profile: "
                "{'driving': 'planned ORS requests 1363 exceed automatic guard 500 "
                "after safe block packing'}"
            )
            payload.update(
                ok=False, paused=True, message=diagnosis,
                failed_tool="build_travel_matrix",
                recovery={
                    "diagnosis": diagnosis, "question": "Adjust routing inputs.",
                    "suggested_changes": {},
                },
            )
        return payload

class _LargePreviewOrchestrator(_RoutingOrchestrator):
    """Publish a quick matrix that is too large for automatic ORS refinement."""

    def run(self, task):
        payload = super().run(task)
        payload["results"][0]["summary"]["mode_reports"] = {
            "walking": {"total_pairs": 500000, "eligible_pairs": 250001}
        }
        return payload


class _PrunedLargePreviewOrchestrator(_RoutingOrchestrator):
    """A large full matrix may proceed when distance pruning keeps ORS work bounded."""

    def run(self, task):
        payload = super().run(task)
        payload["results"][0]["summary"]["mode_reports"] = {
            "walking": {"total_pairs": 471912, "eligible_pairs": 12000}
        }
        return payload


class _ParetoOrchestrator(_RoutingOrchestrator):
    """Publish a compact frontier through the public run result."""

    def run(self, task):
        payload = super().run(task)
        solution = SiteSolution(profile_name="alpha", selected_ids=["site-a"],
                                covered_population=80, covered_ratio=.8,
                                vulnerable_covered_ratio=.7, population_weighted_access_gini=.2,
                                mean_access_score=.6, gradual_coverage_ratio=.6,
                                mean_travel_minutes=8, weights_used=None, solver_status="Optimal")
        point = ParetoPoint(alpha=.5, efficiency_metric=.6, equity_metric=.8,
                            population_weighted_gini=.2, solution=solution)
        self.ctx.put("analysis", {"solution": solution, "points": [point], "frontier": [point]})
        return payload


def _service(tmp_path, monkeypatch, orchestrator=_RoutingOrchestrator) -> OasisService:
    """Create an isolated service with no provider calls."""
    monkeypatch.setattr("oasis.services.application.Orchestrator", orchestrator)
    cfg = {
        "llm": {},
        "accessibility": {},
        "optimization": {},
        "runtime": {"run_db": str(tmp_path / "runs.sqlite")},
        "output": {"dir": str(tmp_path / "outputs")},
    }
    return OasisService(cfg=cfg, llm_factory=lambda: object())


def _terminal(service: OasisService, run_id: str) -> dict:
    """Wait briefly for one deterministic fake run."""
    deadline = time.monotonic() + 3
    while True:
        snapshot = service.run_snapshot(run_id)
        if snapshot["status"] in {
            "cancelled", "superseded", "interrupted", "succeeded", "awaiting_hitl", "failed"
        }:
            return snapshot
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_auto_keeps_quick_preview_and_finishes_with_precise_provenance(tmp_path, monkeypatch):
    """Auto mode must preserve both accuracy phases and a stable spec hash."""
    service = _service(tmp_path, monkeypatch)
    try:
        run_id = service.start_run(TaskSpec(place_name="Phoenix"), "auto")["run_id"]
        snapshot = _terminal(service, run_id)
        assert snapshot["status"] == "succeeded"
        assert snapshot["preview_result"]["execution"]["phase"] == "quick"
        assert snapshot["preview_result"]["execution"]["accuracy"] == "approximate"
        assert snapshot["result"]["execution"]["phase"] == "precise"
        assert snapshot["result"]["execution"]["router_used"] == "ors_sparse"
        quick_report = service.artifact(run_id, "preview_report").read_text(encoding="utf-8")
        precise_report = service.artifact(run_id, "report").read_text(encoding="utf-8")
        assert "Approximate preview (Haversine)" in quick_report
        assert "Precise network result (ORS)" in precise_report
        assert len(snapshot["spec"]["spec_hash"]) == 64
    finally:
        service.executor.shutdown(wait=True)


def test_auto_retains_approximate_result_when_precise_routing_fails(tmp_path, monkeypatch):
    """An ORS failure must not discard a completed approximate map and report."""
    service = _service(tmp_path, monkeypatch, _FallbackOrchestrator)
    try:
        run_id = service.start_run(TaskSpec(place_name="New York"), "auto")["run_id"]
        snapshot = _terminal(service, run_id)
        assert snapshot["status"] == "succeeded"
        assert snapshot["result"] is None
        assert snapshot["preview_result"]["ok"] is True
        assert snapshot["preview_result"]["execution"]["accuracy"] == "approximate"
        assert "approximate selection result has been retained" in snapshot["preview_result"]["message"]
        assert "Point route planning remains available" in snapshot["preview_result"]["message"]
        assert "Precise mode requires ORS" in snapshot["error"]
    finally:
        service.executor.shutdown(wait=True)


def test_auto_retains_preview_when_precise_guard_is_returned_as_agent_failure(tmp_path, monkeypatch):
    """A returned strict-routing failure must not become an unactionable HITL state."""
    service = _service(tmp_path, monkeypatch, _ReturnedRoutingFailureOrchestrator)
    try:
        task = TaskSpec(place_name="Phoenix", mobility_scenario="driving")
        run_id = service.start_run(task, "auto")["run_id"]
        snapshot = _terminal(service, run_id)
        assert snapshot["status"] == "succeeded"
        assert snapshot["preview_result"]["execution"]["accuracy"] == "approximate"
        assert snapshot["preview_result"].get("recovery") is None
        assert "OASIS request-volume guard" in snapshot["preview_result"]["message"]
        assert "reduce the travel threshold" in snapshot["preview_result"]["message"]
    finally:
        service.executor.shutdown(wait=True)

def test_auto_skips_precise_phase_when_quick_pair_count_exceeds_limit(tmp_path, monkeypatch):
    """Large metros must return their preview without repeating an impossible phase."""
    service = _service(tmp_path, monkeypatch, _LargePreviewOrchestrator)
    service.cfg["accessibility"]["ors_max_eligible_pairs"] = 250000
    try:
        run_id = service.start_run(TaskSpec(place_name="New York"), "auto")["run_id"]
        snapshot = _terminal(service, run_id)
        assert snapshot["status"] == "succeeded"
        assert snapshot["preview_result"]["execution"]["accuracy"] == "approximate"
        assert "automatic ORS preflight limit" in snapshot["error"]
    finally:
        service.executor.shutdown(wait=True)


def test_auto_uses_eligible_pairs_not_full_matrix_size_for_ors_preflight(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch, _PrunedLargePreviewOrchestrator)
    service.cfg["accessibility"]["ors_max_eligible_pairs"] = 250000
    try:
        run_id = service.start_run(TaskSpec(place_name="Phoenix"), "auto")["run_id"]
        snapshot = _terminal(service, run_id)
        assert snapshot["status"] == "succeeded"
        assert snapshot["result"]["execution"]["phase"] == "precise"
        assert snapshot["result"]["execution"]["router_used"] == "ors_sparse"
    finally:
        service.executor.shutdown(wait=True)


def test_run_result_exposes_compact_interactive_pareto_points(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch, _ParetoOrchestrator)
    try:
        run_id = service.start_run(TaskSpec(place_name="Phoenix"), "quick")["run_id"]
        point = _terminal(service, run_id)["result"]["pareto_points"][0]
        assert point["alpha"] == .5 and point["gini"] == .2
        assert point["selected_ids"] == ["site-a"] and point["is_frontier"] is True
    finally:
        service.executor.shutdown(wait=True)


@pytest.mark.parametrize("status", ["running", "succeeded"])
def test_point_route_uses_visible_quick_preview(tmp_path, monkeypatch, status):
    """The planner uses the visible preview during refinement and after fallback."""
    service = _service(tmp_path, monkeypatch)
    run_id = "preview-route"
    task = TaskSpec(place_name="Phoenix")
    service.store.create(run_id, {"task": task.model_dump(mode="json"), "mode": "auto"})
    service.store.update(
        run_id,
        status=status,
        preview_result={"artifacts": {"map": f"/v1/runs/{run_id}/artifacts/preview_map"}},
    )
    preview_map = tmp_path / "preview_map.geojson"
    preview_map.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [[
                    [-112.1, 33.4], [-112.0, 33.4], [-112.0, 33.5],
                    [-112.1, 33.5], [-112.1, 33.4],
                ]]},
                "properties": {"layer_kind": "tract", "GEOID": "1", "tract_mean_heat_c": 35},
            },
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-112.05, 33.45]},
                "properties": {"layer_kind": "selected_site", "candidate_id": "site-1"},
            },
        ],
    }), encoding="utf-8")
    service.store.put_artifacts(run_id, "quick", {"preview_map": preview_map})
    captured = {}

    def fake_route(longitude, latitude, areas, candidates, selected_ids, **kwargs):
        captured.update(longitude=longitude, latitude=latitude, selected_ids=selected_ids)
        return {"type": "FeatureCollection", "features": [], "summary": {"destination_id": "site-1"}}

    monkeypatch.setattr("oasis.services.application.plan_point_to_nearest_site", fake_route)
    try:
        result = service.plan_point_route(run_id, -112.05, 33.45, "walking")
        assert result["summary"]["destination_id"] == "site-1"
        assert captured["selected_ids"] == ["site-1"]
    finally:
        service.executor.shutdown(wait=True)


def test_place_route_returns_preview_for_geocoded_origin_outside_study_area(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    run_id = "place-route"
    task = TaskSpec(place_name="Phoenix")
    service.store.create(run_id, {"task": task.model_dump(mode="json"), "mode": "quick"})
    service.store.update(run_id, status="succeeded", result={"artifacts": {}})
    map_path = tmp_path / "map.geojson"
    map_path.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [[
                [-112.1, 33.4], [-112.0, 33.4], [-112.0, 33.5],
                [-112.1, 33.5], [-112.1, 33.4],
            ]]},
            "properties": {"layer_kind": "tract", "GEOID": "1"},
        }],
    }), encoding="utf-8")
    service.store.put_artifacts(run_id, "quick", {"map": map_path})
    monkeypatch.setattr(
        "oasis.services.application.search_origin_candidates",
        lambda query, **kwargs: [{"display_name": "Los Angeles, USA", "lon": "-118.24", "lat": "34.05"}],
    )
    try:
        result = service.plan_place_route(run_id, "Los Angeles", "walking")
        assert result["summary"]["status"] == "outside_study_area"
        assert result["features"][0]["geometry"]["coordinates"] == [-118.24, 34.05]
    finally:
        service.executor.shutdown(wait=True)


def test_precise_mode_returns_an_explicit_approximate_fallback(tmp_path, monkeypatch):
    """A failed precise refinement retains a clearly labelled approximate result."""
    service = _service(tmp_path, monkeypatch, _FallbackOrchestrator)
    try:
        run_id = service.start_run(TaskSpec(place_name="Phoenix"), "precise")["run_id"]
        snapshot = _terminal(service, run_id)
        assert snapshot["status"] == "succeeded"
        assert "requires ORS network routing" in snapshot["error"]
        assert snapshot["result"]["execution"]["accuracy"] == "approximate"
        assert "approximate selection result has been retained" in snapshot["result"]["message"]
        assert "Point route planning remains available" in snapshot["result"]["message"]
    finally:
        service.executor.shutdown(wait=True)


def test_queued_cancel_is_idempotent_and_revision_is_immutable(tmp_path, monkeypatch):
    """Queued work can be cancelled twice and revised into a hashed child spec."""
    service = _service(tmp_path, monkeypatch)
    release = threading.Event()
    blocker = service.executor.submit(release.wait)
    try:
        parent = service.start_run(TaskSpec(place_name="Phoenix"), "quick")["run_id"]
        first = service.cancel_run(parent)
        second = service.cancel_run(parent)
        assert first["status"] in {"cancelling", "cancelled"}
        assert second["status"] in {"cancelling", "cancelled"}
        assert _terminal(service, parent)["status"] == "cancelled"

        old = service.start_run(TaskSpec(place_name="Phoenix", n_facilities=2), "quick")["run_id"]
        child = service.revise_run(
            old, TaskSpec(place_name="Phoenix", n_facilities=3), "quick"
        )
        assert _terminal(service, old)["status"] == "superseded"
        child_snapshot = service.run_snapshot(child["run_id"])
        assert child_snapshot["parent_run_id"] == old
        assert child_snapshot["revision"] == 1
        assert child_snapshot["task"]["n_facilities"] == 3
        assert len(child_snapshot["spec"]["spec_hash"]) == 64
    finally:
        release.set()
        blocker.result(timeout=2)
        service.executor.shutdown(wait=True)


def test_provider_retry_inherits_confirmed_task_and_mode(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    task = TaskSpec(place_name="Los Angeles, California, USA", n_facilities=7, threshold_minutes=12)
    parent = "osm-timeout-parent"
    service.store.create(parent, {"task": task.model_dump(mode="json"), "mode": "precise"})
    service.store.update(parent, status="awaiting_hitl")
    captured = {}
    def queue(spec, upload_id, **kwargs):
        captured.update(spec=spec, upload_id=upload_id, **kwargs)
        return {"run_id": "retry-child", "status": "queued", "mode": spec.mode}
    monkeypatch.setattr(service, "_queue_run", queue)
    try:
        accepted = service.revise_run(parent)
        assert accepted["run_id"] == "retry-child"
        assert captured["spec"].task == task and captured["spec"].mode == "precise"
        assert captured["parent_run_id"] == parent and captured["revision"] == 1
        assert service.store.get(parent)["status"] == "awaiting_hitl"
    finally:
        service.executor.shutdown(wait=True)


def test_run_store_survives_reopen_and_marks_lost_work_interrupted(tmp_path):
    """Persistent state is readable by a new process and active work is not replayed."""
    path = tmp_path / "runs.sqlite"
    first = RunStore(path)
    first.create("run-1", {"mode": "quick", "task": {"place_name": "Phoenix"}})
    first.transition(
        "run-1", ("queued",), "running",
        progress={"label": "Old stage", "done": 1, "total": 1},
    )
    first.append_event("run-1", {"stage": "plan", "message": "queued"})

    second = RunStore(path)
    assert second.interrupt_active() == 1
    snapshot = second.snapshot("run-1")
    assert snapshot["status"] == "interrupted"
    assert snapshot["progress"] is None
    assert snapshot["events"][0]["message"] == "queued"
    assert snapshot["events"][0]["created_at"]


def test_run_store_migrates_existing_events_to_timestamped_rows(tmp_path):
    """Opening an older run database must add event timestamps without data loss."""
    path = tmp_path / "runs.sqlite"
    store = RunStore(path)
    store.create("old-run", {"mode": "quick", "task": {"place_name": "Phoenix"}})
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE run_events")
        connection.execute(
            "CREATE TABLE run_events (run_id TEXT NOT NULL, seq INTEGER NOT NULL, "
            "payload_json TEXT NOT NULL, PRIMARY KEY(run_id,seq))"
        )
        connection.execute(
            "INSERT INTO run_events (run_id,seq,payload_json) VALUES (?,?,?)",
            ("old-run", 1, '{"seq":1,"stage":"legacy","message":"retained"}'),
        )

    migrated = RunStore(path)
    event = migrated.snapshot("old-run")["events"][0]
    assert event["message"] == "retained"
    assert event["created_at"]

def test_run_store_heartbeat_preserves_last_activity_and_updates_elapsed_time(tmp_path):
    """A heartbeat must make a blocked stage observable without inventing activity."""
    path = tmp_path / "runs.sqlite"
    store = RunStore(path)
    store.create("run-heartbeat", {"mode": "quick", "task": {"place_name": "Phoenix"}})
    store.transition(
        "run-heartbeat", ("queued",), "running",
        progress={
            "label": "Minimum-threshold search", "done": 0, "total": 12,
            "unit": "probes", "fraction": 0.0,
            "stage_started_at": "2026-01-01T00:00:00+00:00",
            "activity_at": "2026-01-01T00:00:00+00:00",
        },
    )

    assert store.heartbeat("run-heartbeat")
    progress = store.snapshot("run-heartbeat")["progress"]
    assert progress["label"] == "Minimum-threshold search"
    assert progress["last_activity_at"] == "2026-01-01T00:00:00+00:00"
    assert progress["stage_elapsed_seconds"] > 0
    assert progress["last_activity_seconds"] > 0

def test_waiting_revision_is_visible_and_cancellable(tmp_path, monkeypatch):
    """A serialized run reports why it is waiting and can exit before acquiring the lock."""
    service = _service(tmp_path, monkeypatch)
    service.execution_lock.acquire()
    try:
        run_id = service.start_run(
            TaskSpec(place_name="Phoenix", mobility_scenario="driving"), "auto"
        )["run_id"]
        deadline = time.monotonic() + 2
        while True:
            snapshot = service.run_snapshot(run_id)
            if snapshot["status"] == "running" and snapshot.get("progress"):
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert snapshot["progress"]["label"] == "Waiting for the previous run to stop"
        assert snapshot["task"]["mobility_scenario"] == "driving"

        service.cancel_run(run_id)
        assert _terminal(service, run_id)["status"] == "cancelled"
    finally:
        if service.execution_lock.locked():
            service.execution_lock.release()
        service.executor.shutdown(wait=True)

def test_hard_cancel_terminates_an_isolated_run_process():
    """Whole-run cancellation must not wait for a cooperative stage checkpoint."""
    process = multiprocessing.get_context("spawn").Process(target=time.sleep, args=(30,))
    process.start()
    started = time.monotonic()

    OasisService._terminate_process(process)

    assert not process.is_alive()
    assert time.monotonic() - started < 6


def test_place_route_preserves_scope_and_original_query(tmp_path, monkeypatch):
    service = _service(tmp_path, monkeypatch)
    task = TaskSpec(place_name="Riverside, California, USA")
    run_id = "centre-spelling"
    service.store.create(run_id, {"task": task.model_dump(mode="json"), "mode": "quick"})
    service.store.update(run_id, status="succeeded", result={"artifacts": {}})
    map_path = tmp_path / "map.geojson"
    map_path.write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[-117.4,33.9],[-117.3,33.9],[-117.3,34],[-117.4,34],[-117.4,33.9]]]}, "properties": {"layer_kind": "tract", "GEOID": "1"}}]}), encoding="utf-8")
    service.store.put_artifacts(run_id, "quick", {"map": map_path})
    calls = []
    def geocode(query, **kwargs):
        calls.append((query, kwargs))
        return [{"display_name": "Riverside Convention Center", "lon": "-117.3714766", "lat": "33.9852422"}]
    monkeypatch.setattr("oasis.services.application.search_origin_candidates", geocode)
    monkeypatch.setattr(service, "plan_point_route", lambda *args, **kwargs: {"type": "FeatureCollection", "features": [], "summary": {}})
    try:
        result = service.plan_place_route(run_id, "riverside Convention Centre", "cycling")
        assert len(calls) == 1
        assert all(call[1]["countrycodes"] == "us" and call[1]["viewbox"] == (-117.4,33.9,-117.3,34.0) for call in calls)
        assert result["summary"]["origin_label"] == "Riverside Convention Center"
        assert result["summary"]["origin_query"] == "riverside Convention Centre"
        def unavailable(*args, **kwargs):
            raise RuntimeError("ORS cycling-regular timed out")
        monkeypatch.setattr(service, "plan_point_route", unavailable)
        with pytest.raises(RuntimeError, match="Starting point found: Riverside Convention Center.*Route planning failed: ORS"):
            service.plan_place_route(run_id, "riverside Convention Centre", "cycling")
    finally:
        service.executor.shutdown(wait=True)
