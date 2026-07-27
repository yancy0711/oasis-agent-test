"""LLM orchestration for structured parsing, planning, repair, and explanation."""
from __future__ import annotations
import json, logging, os, pathlib, re
from typing import Any
from agent.memory import Context
from agent.schemas import (AgentAction, AgentPlan, PlanStep, RecoveryProposal, ResultNarrative, TaskSpec,
                           ToolError, ToolResult)
from tools.registry import registry

log = logging.getLogger(__name__)
PROMPT_DIR = pathlib.Path(__file__).parent / "prompts"


class MissingPlaceError(ValueError):
    """Carry a validated partial task when the LLM omitted its study area."""

    def __init__(self, task_data: dict):
        """Store parsed non-place fields for interactive completion."""
        self.task_data = task_data
        super().__init__("Could not parse a place from the request. Include a US city or county, e.g. 'Washington, District of Columbia, USA'.")


def load_prompt(name: str) -> str:
    """Read an agent prompt template by name."""
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


class LLMClient:
    """OpenRouter client using the OpenAI-compatible Chat Completions API."""

    def __init__(self, cfg: dict):
        """Configure the OpenRouter-compatible client and request headers."""
        from openai import OpenAI
        self.cfg = cfg
        if cfg.get("provider") != "openrouter": raise ValueError(f"Unsupported provider: {cfg.get('provider')}. Expected 'openrouter'.")
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key: raise RuntimeError("OPENROUTER_API_KEY is missing. Check the project .env file.")
        headers = {}
        if os.getenv("OPENROUTER_SITE_URL"): headers["HTTP-Referer"] = os.getenv("OPENROUTER_SITE_URL")
        if os.getenv("OPENROUTER_APP_NAME"): headers["X-OpenRouter-Title"] = os.getenv("OPENROUTER_APP_NAME")
        self.client = OpenAI(base_url=cfg.get("base_url", "https://openrouter.ai/api/v1"), api_key=api_key,
                             default_headers=headers or None)

    def structured(self, system: str, user: str) -> dict:
        """Call the LLM with a system/user prompt and parse the JSON output."""
        response = self.client.chat.completions.create(
            model=self.cfg["model"], messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=self.cfg.get("max_tokens", 4096), temperature=self.cfg.get("temperature", 0.0),
            response_format={"type": "json_object"}, extra_body={"provider": {"require_parameters": True}})
        text = response.choices[0].message.content
        if not text: raise ValueError(f"Model {self.cfg['model']} returned empty content")
        return json.loads(text.strip().removeprefix("```json").removesuffix("```").strip())


class Orchestrator:
    """Coordinate typed LLM decisions with deterministic registered tools."""

    def __init__(self, llm: LLMClient, cfg: dict):
        """Create an orchestration session backed by the supplied LLM client."""
        self.llm, self.cfg = llm, cfg
        self.ctx = Context(); self.ctx.put("config", cfg)

    def parse_task(self, user_prompt: str) -> TaskSpec:
        """Parse a request and deterministically fill vague numeric defaults."""
        defaults = {"n_facilities": self.cfg.get("optimization", {}).get("n_facilities", 8),
                    "threshold_minutes": self.cfg.get("accessibility", {}).get("threshold_minutes", 15)}
        runtime = {"defaults": defaults, "value_profiles": self.cfg.get("value_profiles", {})}
        raw = self.llm.structured(load_prompt("task_parsing"),
                                  f"Runtime configuration: {json.dumps(runtime)}\nUser request: {user_prompt}")
        explicit_minutes = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:-\s*)?(?:minutes?|mins?)\b",
                                     user_prompt, re.I)
        if explicit_minutes: raw["threshold_minutes"] = float(explicit_minutes.group(1))
        asks_for_count = re.search(r"\b(how many|minimum|fewest|least number|smallest number)\b", user_prompt, re.I)
        if raw.get("analysis_goal") == "minimum_facilities" and not asks_for_count:
            raw["analysis_goal"] = "facility_siting"
        nullable_defaults = {"intervention": "cooling_center", "analysis_goal": "facility_siting",
                             "candidate_source": "osm", "candidate_amenities": ["library", "community_centre", "school"],
                             "coverage_group": "all", "regional_requirements": [],
                             "preference_question": "", "notes": ""}
        for field, default in nullable_defaults.items():
            if raw.get(field) is None: raw[field] = default
        applied = []
        if isinstance(raw.get("n_facilities"), bool) or not isinstance(raw.get("n_facilities"), int):
            raw["n_facilities"] = defaults["n_facilities"]; applied.append("n_facilities")
        threshold = raw.get("threshold_minutes")
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raw["threshold_minutes"] = defaults["threshold_minutes"]; applied.append("threshold_minutes")
        if applied: raw["notes"] = f"{raw.get('notes', '')} [runtime defaults: {', '.join(applied)}]".strip()
        profiles, weights = runtime["value_profiles"], raw.get("weights")
        known = [profile["weights"] for profile in profiles.values()]
        if weights is not None and weights not in known:
            raw["weights"] = None
            raw["notes"] = f"{raw.get('notes', '')} [unregistered weights require HITL]".strip()
        place = raw.get("place_name")
        self.ctx.record("parse", "Natural-language request parsed into a proposed task.",
                        fields={k: raw.get(k) for k in ("place_name", "analysis_goal", "candidate_source", "n_facilities",
                                                        "threshold_minutes", "coverage_target", "coverage_group",
                                                        "regional_requirements", "preference_question", "notes")})
        if not isinstance(place, str) or not place.strip(): raise MissingPlaceError(raw)
        raw["place_name"] = place.strip()
        return TaskSpec(**raw)

    def _sanitize_step(self, step: PlanStep) -> PlanStep:
        """Drop arguments that are outside a registered tool's public schema."""
        allowed = registry.get(step.tool).args_schema
        args = {key: value for key, value in step.args.items() if key in allowed}
        dropped = sorted(set(step.args) - set(args))
        if dropped: log.warning("Ignoring internal/unknown args for %s: %s", step.tool, ", ".join(dropped))
        return PlanStep(tool=step.tool, args=args, rationale=step.rationale)

    def _bind_task_args(self, step: PlanStep, task: TaskSpec) -> PlanStep:
        """Bind confirmed TaskSpec values to their corresponding tool calls."""
        args = dict(step.args)
        if step.tool == "resolve_study_area": args["place_name"] = task.place_name
        elif step.tool == "fetch_candidate_sites": args["amenities"] = task.candidate_amenities
        elif step.tool == "load_candidate_sites": args["candidate_file"] = task.candidate_file
        elif step.tool == "optimize_sites":
            args.update(n_facilities=task.n_facilities, threshold_minutes=task.threshold_minutes,
                        weights=task.weights.model_dump() if task.weights else None,
                        coverage_target=task.coverage_target, coverage_group=task.coverage_group)
        elif step.tool == "find_minimum_facilities":
            args.update(threshold_minutes=task.threshold_minutes,
                        coverage_target=task.coverage_target if task.coverage_target is not None else 1.0,
                        coverage_group=task.coverage_group)
        elif step.tool == "find_minimum_threshold":
            args.update(n_facilities=task.n_facilities,
                        coverage_target=task.coverage_target if task.coverage_target is not None else 1.0,
                        coverage_group=task.coverage_group)
        elif step.tool == "analyze_reachability":
            args.update(threshold_minutes=task.threshold_minutes, coverage_group=task.coverage_group)
        return PlanStep(tool=step.tool, args=args, rationale=step.rationale)

    def _normalize_plan_payload(self, payload: dict) -> dict:
        """Normalize common JSON key aliases before strict plan validation."""
        steps = payload.get("steps", payload.get("plan"))
        if not isinstance(steps, list): return payload
        normalized = []
        for item in steps:
            if not isinstance(item, dict): normalized.append(item); continue
            step = dict(item)
            if "tool" not in step and "tool_name" in step: step["tool"] = step.pop("tool_name")
            if "args" not in step:
                if "arguments" in step: step["args"] = step.pop("arguments")
                elif "parameters" in step: step["args"] = step.pop("parameters")
            normalized.append(step)
        result = dict(payload); result.pop("plan", None); result["steps"] = normalized
        return result

    def plan(self, task: TaskSpec) -> AgentPlan:
        """Ask the LLM for a task-dependent plan and enforce public argument schemas."""
        user = (f"Task: {task.model_dump_json()}\nAvailable tools: {json.dumps(registry.describe())}\n"
                f"Experience so far:\n{self.ctx.hints()}")
        payload = self._normalize_plan_payload(self.llm.structured(load_prompt("planning"), user))
        plan = AgentPlan(**payload)
        plan.steps = [self._bind_task_args(self._sanitize_step(step), task) for step in plan.steps]
        self.ctx.record("plan", "The model proposed an executable tool plan.",
                        steps=[{"tool": step.tool, "args": step.args, "rationale": step.rationale} for step in plan.steps])
        return plan

    def decide_next(self, task: TaskSpec, results: list[ToolResult], step_index: int) -> AgentAction:
        """Select one registered action from compact state and prior observations."""
        observations = [{"tool": result.tool, "ok": result.ok, "summary": result.summary,
                         "error": result.error.model_dump(mode="json") if result.error else None}
                        for result in results[-8:]]
        state = {"task": task.model_dump(mode="json"), "handles": self.ctx.handles(),
                 "observations": observations, "successful_calls": self.ctx.successes[-8:],
                 "available_tools": registry.describe(),
                 "step_index": step_index}
        payload = self.llm.structured(load_prompt("action_selection"), json.dumps(state))
        if "kind" not in payload and "action" in payload: payload["kind"] = payload.pop("action")
        if "tool" not in payload and "tool_name" in payload: payload["tool"] = payload.pop("tool_name")
        action = AgentAction(**payload)
        if action.kind == "tool":
            if not action.tool: raise ValueError("A tool action requires a registered tool name")
            step = self._bind_task_args(self._sanitize_step(
                PlanStep(tool=action.tool, args=action.args, rationale=action.rationale)), task)
            action.tool, action.args = step.tool, step.args
        else:
            action.tool, action.args = None, {}
        self.ctx.record("decision", action.rationale or action.message or f"Selected {action.kind}.", action.tool,
                        kind=action.kind, args=action.args, step_index=step_index,
                        suggested_changes=action.suggested_changes)
        return action

    def _goal_handle(self, task: TaskSpec) -> str:
        """Return the Context handle that proves the requested analysis is complete."""
        return {"facility_siting": "outputs", "heat_review": "heat_summary",
                "minimum_facilities": "minimum_facilities", "minimum_threshold": "minimum_threshold",
                "reachability_review": "reachability_analysis",
                "scenario_comparison": "scenario_comparison"}[task.analysis_goal]

    def _validated_changes(self, task: TaskSpec, changes: dict[str, Any]) -> dict[str, Any]:
        """Keep only typed human-editable changes proposed by the model."""
        allowed = {"n_facilities", "threshold_minutes", "coverage_target", "candidate_amenities"}
        filtered = {key: value for key, value in changes.items() if key in allowed}
        if not filtered: return {}
        try: validated = TaskSpec(**{**task.model_dump(), **filtered})
        except Exception: return {}
        return {key: getattr(validated, key) for key in filtered}

    def _quality_issue(self, step: PlanStep, result: ToolResult) -> ToolError | None:
        """Turn suspicious successful summaries into explicit agent observations."""
        task = self.ctx.get("task", None)
        if not task: return None
        candidates = result.summary.get("candidates")
        if isinstance(candidates, int) and task.analysis_goal == "facility_siting" and candidates < task.n_facilities:
            supplied = step.tool == "load_candidate_sites" or task.candidate_source == "csv"
            return ToolError(kind="constraint_conflict" if supplied else "insufficient_candidates",
                             message=f"Only {candidates} candidates are available for {task.n_facilities} requested facilities.",
                             hint=("Upload more candidate points or reduce the facility count." if supplied else
                                   "Broaden candidate amenity types or ask the user to reduce the facility count."),
                             retryable=not supplied)
        covered = result.summary.get("covered_tract_ratio")
        if task.coverage_target is not None and isinstance(covered, (int, float)) and covered + 1e-9 < task.coverage_target:
            return ToolError(kind="constraint_conflict",
                             message=f"The solution covers {covered:.1%} of tracts, below the requested {task.coverage_target:.1%}.",
                             hint="Ask whether to increase the facility budget or relax the access threshold.", retryable=False)
        return None

    def execute(self, step: PlanStep) -> ToolResult:
        """Execute a tool and repair retryable failures or suspicious observations."""
        step = self._sanitize_step(step)
        rounds = self.cfg.get("max_self_correction_rounds", 3)
        for attempt in range(rounds + 1):
            self.ctx.record("tool_call", f"Calling {step.tool}.", step.tool, args=step.args, attempt=attempt + 1)
            try: result: ToolResult = registry.get(step.tool).fn(ctx=self.ctx, **step.args)
            except Exception as exc:
                result = ToolResult(ok=False, tool=step.tool, error=ToolError(kind="unknown", message=str(exc)))
            issue = self._quality_issue(step, result) if result.ok else None
            if result.ok and not issue:
                self.ctx.successes.append({"tool": step.tool, "args": step.args, "summary": result.summary})
                self.ctx.record("tool_result", f"{step.tool} completed.", step.tool, ok=True, summary=result.summary)
                return result
            if issue:
                self.ctx.record("observation", issue.message, step.tool, summary=result.summary, hint=issue.hint)
                result = ToolResult(ok=False, tool=step.tool, summary=result.summary, error=issue)
            else:
                self.ctx.record("tool_result", f"{step.tool} failed.", step.tool, ok=False,
                                error=result.error.model_dump() if result.error else {})
            self.ctx.failures.append({"tool": step.tool, "args": step.args,
                                      "error": result.error.message if result.error else ""})
            if attempt == rounds or (result.error and not result.error.retryable): return result
            try: repaired = self._sanitize_step(self._repair(step, result.error))
            except Exception as exc:
                log.warning("repair response rejected: %s", exc)
                self.ctx.record("observation", "The proposed repair failed schema validation; execution stopped safely.",
                                step.tool, repair_error=str(exc))
                return result
            self.ctx.record("repair", f"Retrying after structured feedback: {result.error.kind}.", repaired.tool,
                            previous_args=step.args, repaired_args=repaired.args)
            step = repaired
        return result

    def _repair(self, step: PlanStep, err: ToolError | None) -> PlanStep:
        """Ask the LLM for one repaired tool call after structured feedback."""
        user = (f"Failed or suspicious call: {step.model_dump_json()}\n"
                f"Feedback: {err.model_dump_json() if err else 'unknown'}\n"
                f"Tool description: {registry.get(step.tool).description}")
        return PlanStep(**self.llm.structured(load_prompt("self_correction"), user))

    def propose_recovery(self, task: TaskSpec, result: ToolResult) -> RecoveryProposal:
        """Ask the LLM for a bounded HITL proposal after terminal tool feedback."""
        allowed = {"n_facilities", "threshold_minutes", "coverage_target", "candidate_amenities"}
        evidence = {"task": task.model_dump(mode="json"), "failed_tool": result.tool,
                    "error": result.error.model_dump(mode="json") if result.error else None,
                    "summary": result.summary, "allowed_changes": sorted(allowed)}
        self.ctx.record("hitl", "Requesting a bounded recovery proposal from the model.", result.tool)
        proposal = RecoveryProposal(**self.llm.structured(load_prompt("recovery"), json.dumps(evidence)))
        changes = self._validated_changes(task, proposal.suggested_changes)
        proposal.suggested_changes = changes
        self.ctx.record("hitl", "The recovery proposal is ready for human confirmation.", result.tool,
                        diagnosis=proposal.diagnosis, question=proposal.question,
                        suggested_changes=proposal.suggested_changes)
        return proposal
    def _completion_tool(self, task: TaskSpec) -> str | None:
        """Return the next deterministic dependency or finalizer for a confirmed goal."""
        if task.analysis_goal == "heat_review":
            if not self.ctx.has("study"): return "resolve_study_area"
            if not self.ctx.has("demand"): return "prepare_demand"
            return "summarize_heat_exposure"
        if task.analysis_goal != "facility_siting": return None
        if task.coverage_target is not None and self.ctx.has("travel") and not self.ctx.has("analysis"):
            return "optimize_sites"
        if not self.ctx.has("analysis"): return None
        solution = self.ctx.get("analysis", {}).get("solution")
        return "generate_outputs" if getattr(solution, "solver_status", None) in {"Optimal", "Feasible"} else None

    def _complete_constraint_diagnostics(self, task: TaskSpec, results: list[ToolResult],
                                         steps: list[PlanStep]) -> None:
        """Compute exact relaxation bounds before a hard-coverage failure reaches HITL."""
        if task.coverage_target is None or not all(self.ctx.has(name) for name in ("demand", "candidates", "travel")): return
        for tool, handle in (("find_minimum_facilities", "minimum_facilities"),
                             ("find_minimum_threshold", "minimum_threshold")):
            if self.ctx.has(handle): continue
            step = self._bind_task_args(self._sanitize_step(
                PlanStep(tool=tool, args={}, rationale="Compute an exact bound for the failed hard constraint.")), task)
            steps.append(step)
            self.ctx.record("decision", "A hard-coverage failure requires an exact relaxation bound.",
                            tool, kind="tool", args=step.args, suggested_changes={})
            results.append(self.execute(step))

    def _failure_recovery(self, task: TaskSpec, results: list[ToolResult], question: str = "",
                          safety_limited: bool = False) -> RecoveryProposal:
        """Build a user-visible diagnosis from deterministic errors and bound analyses."""
        error = next((item.error for item in reversed(results) if item.error), None)
        limit = self.cfg.get("max_agent_steps", self.cfg.get("llm", {}).get("max_agent_steps", 14))
        diagnosis = error.message if error else (f"The agent reached its {limit}-action safety limit."
                                                 if safety_limited else "The requested analysis did not produce its required result.")
        if error and error.hint: diagnosis += f" {error.hint}"
        options, changes = [], {}
        facilities = self.ctx.get("minimum_facilities", {})
        minimum = facilities.get("minimum_facilities") if facilities.get("feasible") else None
        if minimum is not None:
            options.append(f"at least {minimum} facilities at {facilities['threshold_minutes']:g} minutes")
            if minimum > task.n_facilities: changes["n_facilities"] = int(minimum)
        threshold = self.ctx.get("minimum_threshold", {})
        minimum_minutes = threshold.get("minimum_threshold_minutes") if threshold.get("feasible") else None
        if minimum_minutes is not None:
            options.append(f"a threshold of at least {minimum_minutes:.1f} minutes with {task.n_facilities} facilities")
            if not changes and minimum_minutes > task.threshold_minutes:
                changes["threshold_minutes"] = float(minimum_minutes)
        if options: diagnosis += " The current candidate set requires " + " or ".join(options) + "."
        prompt = f"Would you like to use {' or '.join(options)}?" if options else (question or "Review the diagnostic and revise the confirmed inputs before retrying.")
        return RecoveryProposal(diagnosis=diagnosis, question=prompt, suggested_changes=changes)
    def interpret_results(self, task: TaskSpec) -> ResultNarrative:
        """Generate two evidence-grounded paragraphs from compact result metadata."""
        analysis = self.ctx.get("analysis")
        evidence = {
            "task": task.model_dump(mode="json"),
            "solution": analysis["solution"].model_dump(mode="json"),
            "frontier": [{"alpha": p.alpha, "efficiency_metric": p.efficiency_metric,
                          "equity_metric": p.equity_metric,
                          "covered_population": p.solution.covered_population} for p in analysis["frontier"]],
            "quality": self.ctx.get("quality", {}),
            "provenance": [p.model_dump(mode="json") for p in self.ctx.lineage],
            "indicators_used": ["population_density", "elderly_ratio", "poverty_rate", "heat_exposure"],
        }
        self.ctx.record("explanation", "Requesting an evidence-grounded interpretation from the model.")
        narrative = ResultNarrative(**self.llm.structured(load_prompt("result_interpretation"), json.dumps(evidence)))
        self.ctx.record("explanation", "Generated an evidence-grounded interpretation and reflection.",
                        decision_summary=narrative.decision_summary, self_reflection=narrative.self_reflection)
        return narrative

    def run(self, task: TaskSpec, explain: bool = True) -> dict[str, Any]:
        """Observe results and select one bounded registered action at a time."""
        self.ctx.put("task", task)
        results: list[ToolResult] = []; steps: list[PlanStep] = []; recovery = None; message = ""
        failed_calls: set[str] = set(); completed_calls: set[str] = set(); repeated_calls: set[str] = set()
        max_steps = self.cfg.get("max_agent_steps", self.cfg.get("llm", {}).get("max_agent_steps", 14))
        goal_handle = self._goal_handle(task); ok, paused, safety_limited = False, False, False
        closure_attempted: set[str] = set()
        for index in range(1, max_steps + 1):
            if self.ctx.has(goal_handle):
                ok = True; message = f"{task.analysis_goal.replace('_', ' ')} completed."
                self.ctx.record("decision", f"Verified completed result handle: {goal_handle}.",
                                kind="finish", args={}, step_index=index, suggested_changes={})
                break
            closure_tool = self._completion_tool(task)
            if closure_tool and closure_tool not in closure_attempted:
                closure_attempted.add(closure_tool)
                step = self._bind_task_args(self._sanitize_step(
                    PlanStep(tool=closure_tool, args={}, rationale="Advance the required dependency chain.")), task)
                steps.append(step)
                self.ctx.record("decision", "The available evidence requires its deterministic finalizer.",
                                step.tool, kind="tool", args={}, step_index=index, suggested_changes={})
                result = self.execute(step); results.append(result)
                signature = json.dumps({"tool": step.tool, "args": step.args}, sort_keys=True)
                (completed_calls if result.ok else failed_calls).add(signature)
                continue
            try: action = self.decide_next(task, results, index)
            except Exception as exc:
                results.append(ToolResult(ok=False, tool="agent_decision",
                                          error=ToolError(kind="schema_mismatch", message=str(exc), retryable=False)))
                self.ctx.record("observation", "The next model action failed typed validation.",
                                "agent_decision", error=str(exc)); break
            if action.kind == "finish":
                if self.ctx.has(self._goal_handle(task)):
                    ok, message = True, action.message; break
                self.ctx.record("observation", "The requested result is not complete; another tool action is required.",
                                missing_handle=self._goal_handle(task)); continue
            if action.kind == "hitl":
                self._complete_constraint_diagnostics(task, results, steps)
                recovery = self._failure_recovery(task, results, action.message)
                if not recovery.suggested_changes:
                    recovery.suggested_changes = self._validated_changes(task, action.suggested_changes)
                self.ctx.record("hitl", "The agent paused for a human-confirmed decision.",
                                diagnosis=recovery.diagnosis, question=recovery.question,
                                suggested_changes=recovery.suggested_changes)
                paused = True; message = recovery.diagnosis; break
            step = PlanStep(tool=action.tool, args=action.args, rationale=action.rationale); steps.append(step)
            signature = json.dumps({"tool": step.tool, "args": step.args}, sort_keys=True, default=str)
            if signature in failed_calls or signature in completed_calls:
                previous = "failed" if signature in failed_calls else "successful"
                duplicate = ToolResult(ok=False, tool=step.tool,
                                       error=ToolError(kind="constraint_conflict",
                                                       message=f"The same {previous} tool call was selected again.",
                                                       hint="Use current handles to choose the next action or request HITL.",
                                                       retryable=False))
                results.append(duplicate); self.ctx.record("observation", duplicate.error.message,
                                                           step.tool, args=step.args)
                if signature in repeated_calls: break
                repeated_calls.add(signature); continue
            result = self.execute(step); results.append(result)
            (completed_calls if result.ok else failed_calls).add(signature)
        else:
            safety_limited = True
            self.ctx.record("observation", f"The agent reached its {max_steps}-action safety limit.")

        if not ok and recovery is None:
            self._complete_constraint_diagnostics(task, results, steps)
            recovery = self._failure_recovery(task, results, safety_limited=safety_limited)
            paused = True; message = recovery.diagnosis
            self.ctx.record("hitl", "The incomplete run produced a user-visible diagnostic.",
                            diagnosis=recovery.diagnosis, question=recovery.question,
                            suggested_changes=recovery.suggested_changes)

        narrative = None
        if ok and explain and self.ctx.has("analysis") and self.ctx.has("outputs"):
            try:
                narrative = self.interpret_results(task)
                from output.report import append_agent_narrative
                append_agent_narrative(self.ctx.get("outputs")["report"], narrative)
            except Exception as exc: log.warning("result interpretation skipped: %s", exc)
        diagnostic_handles = ["constraint_validation", "minimum_facilities", "minimum_threshold",
                              "reachability_analysis", "scenario_comparison"]
        diagnostics = {name: json.loads(json.dumps(self.ctx.get(name), default=lambda value: value.model_dump(mode="json")))
                       for name in diagnostic_handles if self.ctx.has(name)}
        plan = AgentPlan(steps=steps, reasoning="Actions were selected from observations one step at a time.")
        failed = next((result.tool for result in reversed(results) if not result.ok), None)
        return {"ok": ok, "paused": paused, "message": message,
                "failed_tool": None if ok else (failed or ("hitl" if paused else "agent_loop")),
                "plan": plan, "results": results, "lineage": self.ctx.lineage,
                "outputs": self.ctx.get("outputs", None), "heat_summary": self.ctx.get("heat_summary", None),
                "diagnostics": diagnostics, "narrative": narrative, "recovery": recovery,
                "history": self.ctx.history}
