"""Orchestration layer -- the ONLY place in the codebase that talks to an LLM.

Iron rule: the LLM outputs (a) structured task specs, (b) tool-call plans,
(c) error repairs. It never generates or mutates data values.

Session memory lives in agent/memory.py (Context): large objects stay out
of the LLM context window; recent successes/failures become prompt hints.
"""
from __future__ import annotations
import json, logging, pathlib
import os
from typing import Any
from agent.memory import Context
from agent.schemas import AgentPlan, PlanStep, TaskSpec, ToolError, ToolResult
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



# ---------- LLM client (thin, provider-swappable) ----------
class LLMClient:
    """OpenRouter client using the OpenAI-compatible Chat Completions API."""

    def __init__(self, cfg: dict):
        """Configure the OpenRouter-compatible client and request headers."""
        from openai import OpenAI

        self.cfg = cfg

        if cfg.get("provider") != "openrouter":
            raise ValueError(
                f"Unsupported provider: {cfg.get('provider')}. "
                "Expected 'openrouter'."
            )

        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY is missing. "
                "Check the project .env file."
            )

        headers = {}

        site_url = os.getenv("OPENROUTER_SITE_URL")
        app_name = os.getenv("OPENROUTER_APP_NAME")

        if site_url:
            headers["HTTP-Referer"] = site_url
        if app_name:
            headers["X-OpenRouter-Title"] = app_name

        self.client = OpenAI(
            base_url=cfg.get(
                "base_url",
                "https://openrouter.ai/api/v1",
            ),
            api_key=api_key,
            default_headers=headers or None,
        )

    def structured(self, system: str, user: str) -> dict:
        """Call the LLM with a system/user prompt and parse the JSON output."""
        response = self.client.chat.completions.create(
            model=self.cfg["model"],
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=self.cfg.get("max_tokens", 4096),
            temperature=self.cfg.get("temperature", 0.0),
            response_format={"type": "json_object"},
            extra_body={
                "provider": {
                    "require_parameters": True,
                }
            },
        )

        text = response.choices[0].message.content
        if not text:
            raise ValueError(
                f"Model {self.cfg['model']} returned empty content"
            )

        cleaned = (
            text.strip()
            .removeprefix("```json")
            .removesuffix("```")
            .strip()
        )

        return json.loads(cleaned)


# ---------- orchestrator ----------
class Orchestrator:
    def __init__(self, llm: LLMClient, cfg: dict):
        """Create an orchestration session backed by the supplied LLM client."""
        self.llm, self.cfg = llm, cfg
        self.ctx = Context()
        self.ctx.put("config", cfg)

    def parse_task(self, user_prompt: str) -> TaskSpec:
        """Parse a request and deterministically fill vague numeric defaults."""
        defaults = {"n_facilities": self.cfg.get("optimization", {}).get("n_facilities", 8),
                    "threshold_minutes": self.cfg.get("accessibility", {}).get("threshold_minutes", 15)}
        runtime = {"defaults": defaults, "value_profiles": self.cfg.get("value_profiles", {})}
        raw = self.llm.structured(load_prompt("task_parsing"),
                                  f"Runtime configuration: {json.dumps(runtime)}\nUser request: {user_prompt}")
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
        elif step.tool == "optimize_sites":
            args.update(n_facilities=task.n_facilities, threshold_minutes=task.threshold_minutes,
                        weights=task.weights.model_dump() if task.weights else None)
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
        """Ask the LLM for a plan and enforce each tool's public argument schema."""
        user = (f"Task: {task.model_dump_json()}\n"
                f"Available tools: {json.dumps(registry.describe())}\n"
                f"Experience so far:\n{self.ctx.hints()}")
        payload = self._normalize_plan_payload(self.llm.structured(load_prompt("planning"), user))
        plan = AgentPlan(**payload)
        plan.steps = [self._bind_task_args(self._sanitize_step(step), task) for step in plan.steps]
        return plan

    def execute(self, step: PlanStep) -> ToolResult:
        """Capability 3: self-correction loop. Tools return structured errors;
        the LLM reads them and proposes a repaired call, up to a bounded
        number of rounds (protects both quota and demo time)."""
        step = self._sanitize_step(step)
        for attempt in range(self.cfg.get("max_self_correction_rounds", 3) + 1):
            try:
                result: ToolResult = registry.get(step.tool).fn(ctx=self.ctx, **step.args)
            except Exception as e:   # last-resort net: raw exception -> structured error
                result = ToolResult(ok=False, tool=step.tool,
                                    error=ToolError(kind="unknown", message=str(e)))
            if result.ok:
                self.ctx.successes.append({"tool": step.tool, "args": step.args})
                return result
            self.ctx.failures.append({"tool": step.tool, "args": step.args,
                                      "error": result.error.message if result.error else ""})
            if attempt == self.cfg.get("max_self_correction_rounds", 3) or \
               (result.error and not result.error.retryable):
                return result
            step = self._sanitize_step(self._repair(step, result.error))
            log.info("self-correct round %d -> %s", attempt + 1, step.args)
        return result

    def _repair(self, step: PlanStep, err: ToolError | None) -> PlanStep:
        """Ask the LLM for one repaired tool call after a structured error."""
        user = (f"Failed call: {step.model_dump_json()}\n"
                f"Error: {err.model_dump_json() if err else 'unknown'}\n"
                f"Tool description: {registry.get(step.tool).description}")
        return PlanStep(**self.llm.structured(load_prompt("self_correction"), user))

    def run(self, task: TaskSpec) -> dict[str, Any]:
        """Plan and execute every tool step for an already parsed task."""
        results = []
        for step in self.plan(task).steps:
            r = self.execute(step)
            results.append(r)
            if not r.ok:
                log.error("step %s ultimately failed", step.tool)
                break
        return {"results": results, "lineage": self.ctx.lineage}
