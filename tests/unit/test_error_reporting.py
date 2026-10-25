"""Regression tests for actionable failure reporting."""
from oasis.agent.orchestrator import Orchestrator
from oasis.contracts.domain import TaskSpec, ToolError, ToolResult, ValueWeights


def test_duplicate_guard_does_not_hide_original_provider_error():
    """HITL diagnosis must retain the actionable data-provider failure."""
    orch = Orchestrator(object(), {})
    task = TaskSpec(place_name="Austin, Texas, USA", weights=ValueWeights())
    results = [
        ToolResult(
            ok=False,
            tool="prepare_demand",
            error=ToolError(
                kind="unknown",
                message="Landsat heat exposure acquisition failed: unexpected EOF",
                retryable=False,
            ),
        ),
        ToolResult(
            ok=False,
            tool="prepare_demand",
            error=ToolError(
                kind="constraint_conflict",
                message="The same failed tool call was selected again.",
            ),
        ),
    ]

    recovery = orch._failure_recovery(task, results)
    assert recovery.diagnosis == "Landsat heat exposure acquisition failed: unexpected EOF"


def test_provider_outage_stops_before_llm_can_replace_it_with_csv(monkeypatch):
    from oasis.services import workflow
    from oasis.core.data.overpass import OverpassUnavailable
    orch = Orchestrator(object(), {})  # No LLM is needed to diagnose an outage.
    orch.ctx.put("study", {"boundary": None, "cache_scope": "test_provider_outage"})
    orch.ctx.put("demand", {})
    def outage(*args, **kwargs):
        raise OverpassUnavailable("OpenStreetMap place lookup failed (502)")
    monkeypatch.setattr(workflow, "fetch_candidates", outage)
    task = TaskSpec(place_name="Riverside, California", analysis_goal="reachability_review", weights=ValueWeights())
    result = orch.run(task, explain=False)
    assert result["paused"] and not result["ok"]
    assert result["failed_tool"] == "fetch_candidate_sites"
    assert "502" in result["recovery"].diagnosis
    assert "optional, not required" in result["recovery"].question
    assert [step.tool for step in result["plan"].steps] == ["fetch_candidate_sites"]


def test_candidate_source_is_bound_to_confirmed_task():
    from oasis.contracts.domain import PlanStep
    orch = Orchestrator(object(), {})
    osm_task = TaskSpec(place_name="Riverside", weights=ValueWeights())
    step = orch._bind_task_args(PlanStep(tool="load_candidate_sites", args={"candidate_file": "invented.csv"}), osm_task)
    assert step.tool == "fetch_candidate_sites" and "candidate_file" not in step.args
    csv_task = osm_task.model_copy(update={"candidate_source": "csv", "candidate_file": "confirmed.csv"})
    step = orch._bind_task_args(PlanStep(tool="fetch_candidate_sites", args={}), csv_task)
    assert step.tool == "load_candidate_sites" and step.args["candidate_file"] == "confirmed.csv"
