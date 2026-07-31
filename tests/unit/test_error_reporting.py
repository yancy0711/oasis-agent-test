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
