"""CLI entry for deterministic and agent-driven OASIS workflows."""
from __future__ import annotations
import argparse, logging
from cli import hitl

from oasis.agent.orchestrator import LLMClient, MissingPlaceError, Orchestrator
from oasis.contracts.domain import TaskSpec
from oasis.settings import load_cfg
from oasis.settings import get_weights, load_profiles
from oasis.services.workflow import run_tool_chain

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")




def run_pipeline(cfg: dict, place: str, profile: str | None = None,
                 interactive: bool = False, validate_place: bool = True,
                 n_facilities: int | None = None, threshold_minutes: float | None = None) -> dict:
    """Confirm geography, then run the deterministic six-tool pipeline."""
    opt, access = cfg.get("optimization", {}), cfg.get("accessibility", {})
    selected_profile = profile or opt.get("value_profile", "balanced")
    task = TaskSpec(place_name=place, n_facilities=n_facilities if n_facilities is not None else opt.get("n_facilities", 8),
                    threshold_minutes=threshold_minutes if threshold_minutes is not None else access.get("threshold_minutes", 15),
                    weights=get_weights(selected_profile), notes=f"value profile: {selected_profile}")
    if validate_place: task = hitl.clarify_place(task, interactive=interactive)
    result = run_tool_chain(cfg, task)
    if result["ok"]: logging.info("outputs: %s", result["outputs"])
    else: logging.error("pipeline stopped at %s: %s", result["failed_tool"], result["results"][-1].error)
    return result


def run_agent(cfg: dict, prompt: str, model_override: str | None = None,
              llm=None, interactive: bool = True, validate_place: bool = True,
              n_facilities: int | None = None, threshold_minutes: float | None = None,
              profile: str | None = None) -> dict:
    """Natural language -> parse -> HITL -> bounded observe-decide-act loop."""
    if not prompt.strip(): raise ValueError("--prompt is required in agent mode")
    llm_cfg = dict(cfg["llm"])
    if model_override: llm_cfg["model"] = model_override
    agent_cfg = dict(cfg); agent_cfg["max_self_correction_rounds"] = llm_cfg.get("max_self_correction_rounds", 3)
    agent_cfg["value_profiles"] = load_profiles()
    orch = Orchestrator(llm or LLMClient(llm_cfg), agent_cfg)
    try: task = orch.parse_task(prompt)
    except MissingPlaceError as exc:
        if not interactive: raise
        print(f"\n{exc}")
        place = input("Enter a US city or county: ").strip()
        if not place: raise ValueError("A place is required before the agent can continue")
        data = {**exc.task_data, "place_name": place,
                "notes": f"{exc.task_data.get('notes', '')} [place supplied by user]".strip()}
        task = TaskSpec(**data)
    overrides, labels = {}, []
    if n_facilities is not None: overrides["n_facilities"] = n_facilities; labels.append("n_facilities")
    if threshold_minutes is not None: overrides["threshold_minutes"] = threshold_minutes; labels.append("threshold_minutes")
    if profile: overrides["weights"] = get_weights(profile); labels.append(f"profile={profile}")
    if overrides:
        overrides["notes"] = f"{task.notes} [CLI overrides: {', '.join(labels)}]".strip()
        task = TaskSpec(**{**task.model_dump(), **overrides})
    if validate_place: task = hitl.clarify_place(task, interactive=interactive)
    task = hitl.clarify(task, interactive=interactive)
    logging.info("agent task: place=%s; n=%d; threshold=%g min; weights=%s", task.place_name,
                 task.n_facilities, task.threshold_minutes,
                 task.weights.model_dump() if task.weights else None)
    orch.ctx.record("hitl", "Human-confirmed task parameters were bound before execution.",
                    place_name=task.place_name, n_facilities=task.n_facilities,
                    threshold_minutes=task.threshold_minutes,
                    weights=task.weights.model_dump() if task.weights else None)
    return {"task": task, **orch.run(task)}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["pipeline", "agent"], default="pipeline")
    ap.add_argument("--place", default="Los Angeles County, California, USA")
    ap.add_argument("--prompt", default="")
    ap.add_argument("--model", default=None, help="Override the OpenRouter model ID from settings.yaml")
    ap.add_argument("--n-facilities", type=int, default=None, help="Override optimization.n_facilities")
    ap.add_argument("--threshold-minutes", type=float, default=None, help="Override the walking-time threshold")
    ap.add_argument("--profile", choices=load_profiles(), default=None,
                    help="Override the value profile in pipeline or agent mode")
    a, cfg = ap.parse_args(), load_cfg()
    if a.mode == "agent":
        try:
            result = run_agent(cfg, a.prompt, a.model, n_facilities=a.n_facilities,
                               threshold_minutes=a.threshold_minutes, profile=a.profile)
        except ValueError as exc:
            logging.error("agent input stopped: %s", exc)
            raise SystemExit(2)
        if result.get("message"): logging.info("agent answer: %s", result["message"])
        if result.get("diagnostics"): logging.info("diagnostics: %s", result["diagnostics"])
        if result["outputs"]: logging.info("outputs: %s", result["outputs"])
        if not result["ok"]: raise SystemExit(1)
    else:
        result = run_pipeline(cfg, a.place, a.profile, interactive=True,
                              n_facilities=a.n_facilities, threshold_minutes=a.threshold_minutes)
        if not result["ok"]: raise SystemExit(1)
