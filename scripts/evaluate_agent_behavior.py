"""Run an explicit live-LLM planning and self-correction evaluation to validate if the model is using the tool chain as expected and if it can self-correct when a tool fails."""
from __future__ import annotations
import json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from agent.orchestrator import LLMClient, Orchestrator
from agent.schemas import (PlanStep, RegionalRequirement, TaskSpec, ToolError,
                           ToolResult, ValueWeights)
from hitl.knobs import load_profiles
from main import load_cfg
from tools.registry import registry
from tools.workflow import TOOL_CHAIN


def _event_dicts(orch: Orchestrator) -> list[dict]:
    """Return JSON-safe audit events for one evaluation case."""
    return [event.model_dump(mode="json") for event in orch.ctx.history]


def _evaluate_plan(client: LLMClient, cfg: dict, name: str, prompt: str) -> dict:
    """Parse one prompt and record the model-proposed tool sequence."""
    orch = Orchestrator(client, cfg)
    try:
        task = orch.parse_task(prompt); plan = orch.plan(task)
        return {"case": name, "prompt": prompt, "task": task.model_dump(mode="json"),
                "tools": [step.tool for step in plan.steps], "history": _event_dicts(orch), "error": None}
    except Exception as exc:
        return {"case": name, "prompt": prompt, "task": None, "tools": [],
                "history": _event_dicts(orch), "error": f"{type(exc).__name__}: {exc}"}


def _evaluate_action(client: LLMClient, cfg: dict, name: str, task: TaskSpec,
                     observations: list[ToolResult] | None = None) -> dict:
    """Ask the live model for one action from a synthetic tool-ready state."""
    orch = Orchestrator(client, cfg)
    for handle in ("study", "demand", "candidates", "travel"): orch.ctx.put(handle, {})
    try:
        action = orch.decide_next(task, observations or [], 1)
        return {"case": name, "action": action.model_dump(mode="json"), "history": _event_dicts(orch), "error": None}
    except Exception as exc:
        return {"case": name, "action": None, "history": _event_dicts(orch),
                "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    """Run live branching and observation-driven repair checks and save JSON evidence."""
    load_dotenv(ROOT / ".env")
    cfg = load_cfg(); cfg = {**cfg, "value_profiles": load_profiles(),
                             "max_self_correction_rounds": cfg["llm"].get("max_self_correction_rounds", 3)}
    client = LLMClient(cfg["llm"])
    cases = [
        ("full_siting", "Site 8 cooling centers in Phoenix, Arizona within a 15-minute walking threshold."),
        ("provided_candidates", "I already have candidate sites in candidates.csv. Select 8 sites in Phoenix, Arizona from that CSV."),
        ("heat_only", "Do not optimize sites. Only show which Phoenix communities have the highest heat exposure."),
    ]
    plans = [_evaluate_plan(client, cfg, name, prompt) for name, prompt in cases]
    by_name = {case["case"]: case for case in plans}
    checks = {
        "full_siting_uses_baseline": by_name["full_siting"]["tools"] == TOOL_CHAIN,
        "csv_skips_osm": "load_candidate_sites" in by_name["provided_candidates"]["tools"] and
                         "fetch_candidate_sites" not in by_name["provided_candidates"]["tools"],
        "heat_skips_mclp": "summarize_heat_exposure" in by_name["heat_only"]["tools"] and
                           "optimize_sites" not in by_name["heat_only"]["tools"],
        "plans_really_branch": len({tuple(case["tools"]) for case in plans}) == 3,
    }

    action_cases = [
        ("regional_contradiction", TaskSpec(place_name="Phoenix, Arizona, USA", n_facilities=2,
                                                   regional_requirements=[RegionalRequirement(region="north", minimum_facilities=3)]),
         "validate_constraints", []),
        ("fixed_budget", TaskSpec(place_name="Phoenix, Arizona, USA", analysis_goal="facility_siting",
                                  n_facilities=2, threshold_minutes=15, weights=ValueWeights()), "optimize_sites", []),
        ("minimum_facilities", TaskSpec(place_name="Phoenix, Arizona, USA", analysis_goal="minimum_facilities",
                                        threshold_minutes=15, coverage_target=1.0), "find_minimum_facilities", []),
        ("minimum_threshold", TaskSpec(place_name="Phoenix, Arizona, USA", analysis_goal="minimum_threshold",
                                       n_facilities=2, coverage_target=1.0), "find_minimum_threshold", []),
        ("hardest_tract", TaskSpec(place_name="Phoenix, Arizona, USA", analysis_goal="reachability_review"),
         "analyze_reachability", []),
        ("fixed_threshold_recovery", TaskSpec(place_name="Phoenix, Arizona, USA", analysis_goal="facility_siting",
                                               n_facilities=2, threshold_minutes=15, coverage_target=1.0,
                                               weights=ValueWeights(), notes="15-minute threshold is non-negotiable"),
         "find_minimum_facilities", [ToolResult(ok=False, tool="optimize_sites",
             error=ToolError(kind="constraint_conflict", message="infeasible", retryable=False))]),
    ]
    actions = [_evaluate_action(client, cfg, name, task, observations)
               for name, task, _, observations in action_cases]
    expected = {name: tool for name, _, tool, _ in action_cases}
    checks["semantic_tool_selection"] = all(
        item["action"] and item["action"]["kind"] == "tool" and
        item["action"]["tool"] == expected[item["case"]] for item in actions)

    calls = []

    @registry.register("live_candidate_probe", "Search public candidate amenities; broaden amenities when the count is too small.",
                       {"amenities": {"type": "array", "items": {"type": "string"}}})
    def candidate_probe(ctx, amenities):
        """Return a suspicious success once, then a sufficient candidate count."""
        calls.append(amenities)
        return ToolResult(ok=True, tool="live_candidate_probe",
                          summary={"candidates": 3 if len(calls) == 1 else 12})

    repair = Orchestrator(client, {**cfg, "max_self_correction_rounds": 1})
    repair.ctx.put("task", TaskSpec(place_name="Test City, USA", n_facilities=8, weights=ValueWeights()))
    repair_result = repair.execute(PlanStep(tool="live_candidate_probe", args={"amenities": ["library"]}))
    repair_stages = [event.stage for event in repair.ctx.history]
    checks["live_self_correction"] = repair_result.ok and len(calls) == 2 and calls[0] != calls[1] and "repair" in repair_stages

    report = {"model": cfg["llm"]["model"], "fixed_pipeline": TOOL_CHAIN, "plans": plans, "actions": actions,
              "repair": {"ok": repair_result.ok, "calls": calls, "history": _event_dicts(repair)},
              "checks": checks, "all_passed": all(checks.values())}
    out = ROOT / "outputs_run" / "agent_behavior_evaluation.json"; out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"model": report["model"], "checks": checks, "record": str(out)}, indent=2))
    return 0 if report["all_passed"] else 1


if __name__ == "__main__": raise SystemExit(main())
