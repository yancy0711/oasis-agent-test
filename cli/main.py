"""CLI entry for deterministic and agent-driven OASIS workflows."""
from __future__ import annotations

import argparse
import contextlib
import io
import logging
from collections.abc import Callable
from typing import Any

from cli import hitl
from oasis.agent.orchestrator import LLMClient, MissingPlaceError, Orchestrator
from oasis.contracts.domain import TaskSpec
from oasis.services.workflow import run_tool_chain
from oasis.settings import get_weights, load_cfg, load_profiles


class _WarningCollector(logging.Handler):
    """Collect warnings for one short end-of-run summary."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.messages.append(record.getMessage())
        except Exception:
            return


def _configure_logging(verbose: bool) -> _WarningCollector:
    """Keep normal CLI output quiet while retaining an opt-in debug view."""
    collector = _WarningCollector()
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(collector)
    root.setLevel(logging.INFO if verbose else logging.WARNING)
    logging.raiseExceptions = verbose
    if verbose:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(console)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return collector


def _quiet_call(fn: Callable[[], Any], verbose: bool) -> Any:
    """Hide library progress bars and worker diagnostics in normal mode."""
    if verbose:
        return fn()
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        return fn()


def run_pipeline(
    cfg: dict,
    place: str,
    profile: str | None = None,
    interactive: bool = False,
    validate_place: bool = True,
    n_facilities: int | None = None,
    threshold_minutes: float | None = None,
    verbose: bool = False,
) -> dict:
    """Confirm geography, then run the deterministic seven-tool pipeline."""
    opt, access = cfg.get("optimization", {}), cfg.get("accessibility", {})
    selected_profile = profile or opt.get("value_profile", "balanced")
    task = TaskSpec(
        place_name=place,
        n_facilities=n_facilities if n_facilities is not None else opt.get("n_facilities", 10),
        threshold_minutes=(
            threshold_minutes if threshold_minutes is not None else access.get("threshold_minutes", 15)
        ),
        weights=get_weights(selected_profile),
        notes=f"value profile: {selected_profile}",
    )
    if validate_place:
        task = hitl.clarify_place(task, interactive=interactive)
    return _quiet_call(lambda: run_tool_chain(cfg, task), verbose)


def run_agent(
    cfg: dict,
    prompt: str,
    model_override: str | None = None,
    llm=None,
    interactive: bool = True,
    validate_place: bool = True,
    n_facilities: int | None = None,
    threshold_minutes: float | None = None,
    profile: str | None = None,
    verbose: bool = False,
) -> dict:
    """Natural language -> parse -> HITL -> bounded observe-decide-act loop."""
    if not prompt.strip():
        raise ValueError("--prompt is required in agent mode")
    llm_cfg = dict(cfg["llm"])
    if model_override:
        llm_cfg["model"] = model_override
    agent_cfg = dict(cfg)
    agent_cfg["max_self_correction_rounds"] = llm_cfg.get("max_self_correction_rounds", 3)
    agent_cfg["value_profiles"] = load_profiles()
    orch = Orchestrator(llm or LLMClient(llm_cfg), agent_cfg)
    try:
        task = _quiet_call(lambda: orch.parse_task(prompt), verbose)
    except MissingPlaceError as exc:
        if not interactive:
            raise
        print(f"\n{exc}")
        place = input("Enter a US city or county: ").strip()
        if not place:
            raise ValueError("A place is required before the agent can continue")
        data = {
            **exc.task_data,
            "place_name": place,
            "notes": f"{exc.task_data.get('notes', '')} [place supplied by user]".strip(),
        }
        task = TaskSpec(**data)
    overrides, labels = {}, []
    if n_facilities is not None:
        overrides["n_facilities"] = n_facilities
        labels.append("n_facilities")
    if threshold_minutes is not None:
        overrides["threshold_minutes"] = threshold_minutes
        labels.append("threshold_minutes")
    if profile:
        overrides["weights"] = get_weights(profile)
        labels.append(f"profile={profile}")
    if overrides:
        overrides["notes"] = f"{task.notes} [CLI overrides: {', '.join(labels)}]".strip()
        task = TaskSpec(**{**task.model_dump(), **overrides})
    if validate_place:
        task = hitl.clarify_place(task, interactive=interactive)
    task = hitl.clarify(task, interactive=interactive)
    logging.info(
        "agent task: place=%s; n=%d; threshold=%g min; weights=%s",
        task.place_name,
        task.n_facilities,
        task.threshold_minutes,
        task.weights.model_dump() if task.weights else None,
    )
    orch.ctx.record(
        "hitl",
        "Human-confirmed task parameters were bound before execution.",
        place_name=task.place_name,
        n_facilities=task.n_facilities,
        threshold_minutes=task.threshold_minutes,
        weights=task.weights.model_dump() if task.weights else None,
    )
    return {"task": task, **_quiet_call(lambda: orch.run(task), verbose)}


def _friendly_warning(message: str) -> str | None:
    """Collapse implementation warnings into actionable user language."""
    flat = " ".join(str(message).split())
    lower = flat.lower()
    if not flat or "handle is closed" in lower:
        return None
    if ("planned requests" in lower and "guard" in lower) or "eligible pairs exceed ors limit" in lower:
        return (
            "The full ORS matrix exceeded its bounded request budget. The completed analysis used "
            "the documented approximate routing fallback; point-route planning remains available in the web app."
        )
    if "routing backend unavailable" in lower and "fallback" in lower:
        return "Network routing was unavailable, so the completed analysis used the documented approximate fallback."
    if "population weighting unavailable" in lower:
        return "Fine-grid population weighting was unavailable; the report records the tract-level fallback used."
    if "landsat" in lower and ("failed" in lower or "unavailable" in lower):
        return "Some Landsat processing was unavailable; the report records the retained heat-data fallback."
    if len(flat) > 240:
        return flat[:237].rstrip() + "..."
    return flat


def _summarize_warnings(messages: list[str]) -> list[str]:
    warnings: list[str] = []
    for raw in messages:
        friendly = _friendly_warning(raw)
        if friendly and friendly not in warnings:
            warnings.append(friendly)
    return warnings[:5]


def _error_text(result: dict) -> str:
    """Extract one readable failure without dumping internal objects."""
    results = result.get("results") or []
    error = getattr(results[-1], "error", None) if results else result.get("error")
    if error is None:
        return "The analysis did not complete. Run again with --verbose for diagnostic details."
    message = getattr(error, "message", None) or str(error)
    hint = getattr(error, "hint", None)
    return f"{message} {hint or ''}".strip()


def _print_summary(result: dict, warnings: list[str]) -> None:
    """Print only the outcome, useful warnings, and artifact locations."""
    print()
    if result.get("ok"):
        print("[OK] Analysis completed.")
        if result.get("message"):
            print(f"Result: {result['message']}")
    else:
        print("[FAILED] Analysis did not complete.")
        print(f"Reason: {_error_text(result)}")

    if warnings:
        print("\nWarnings:")
        for warning in warnings:
            print(f"  - {warning}")

    outputs = result.get("outputs") or {}
    if outputs:
        labels = {"map": "Map data", "pareto": "Pareto chart", "report": "Report"}
        print("\nOutputs:")
        for key, value in outputs.items():
            print(f"  {labels.get(key, key.replace('_', ' ').title())}: {value}")

    print("\nFor interactive maps, Pareto inspection, comparisons, and point routing, use the web app.")
    print("Run the API with `python -m uvicorn api.main:app --port 8000`, then `npm run dev` in frontend/.")


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return a shell-friendly status code."""
    ap = argparse.ArgumentParser(description="Run an OASIS analysis with concise terminal output.")
    ap.add_argument("--mode", choices=["pipeline", "agent"], default="pipeline")
    ap.add_argument("--place", default="Los Angeles County, California, USA")
    ap.add_argument("--prompt", default="")
    ap.add_argument("--model", default=None, help="Override the model ID from settings.yaml")
    ap.add_argument("--n-facilities", type=int, default=None, help="Override optimization.n_facilities")
    ap.add_argument("--threshold-minutes", type=float, default=None, help="Override the travel-time threshold")
    ap.add_argument(
        "--profile",
        choices=load_profiles(),
        default=None,
        help="Override the value profile in pipeline or agent mode",
    )
    ap.add_argument("--verbose", action="store_true", help="Show progress, network logs, and debug diagnostics")
    args = ap.parse_args(argv)
    collector = _configure_logging(args.verbose)
    try:
        cfg = load_cfg()
        if args.mode == "agent":
            result = run_agent(
                cfg,
                args.prompt,
                args.model,
                n_facilities=args.n_facilities,
                threshold_minutes=args.threshold_minutes,
                profile=args.profile,
                verbose=args.verbose,
            )
        else:
            result = run_pipeline(
                cfg,
                args.place,
                args.profile,
                interactive=True,
                n_facilities=args.n_facilities,
                threshold_minutes=args.threshold_minutes,
                verbose=args.verbose,
            )
    except KeyboardInterrupt:
        print("\n[CANCELLED] Analysis stopped by the user.")
        return 130
    except Exception as exc:
        if args.verbose:
            logging.exception("CLI execution failed")
        result = {"ok": False, "error": str(exc), "outputs": {}}
    _print_summary(result, _summarize_warnings(collector.messages))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
