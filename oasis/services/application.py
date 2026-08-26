"""Application services and a bounded in-memory run queue for the HTTP API."""
from __future__ import annotations
import hashlib
import ipaddress
import json
import multiprocessing
import os
import socket
import signal
import subprocess
import requests
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from fastapi.encoders import jsonable_encoder

from oasis.agent.orchestrator import LLMClient, MissingPlaceError, Orchestrator
from oasis.agent.scope import classify_request
from oasis.contracts.domain import (AgentEvent, ExecutionProvenance, RoutingMode,
                                    RunSpec, TaskSpec)
from oasis.core.cancellation import CancellationToken, RunCancelled
from oasis.settings import PROJECT_ROOT, load_cfg
from oasis.settings import load_profiles
from oasis.core.outputs.report import render_diagnostic_report
from oasis.core.data.geocoding import (AmbiguousPlaceError, cache_place_candidates,
                                       canonical_place_name, route_origin_options, search_origin_candidates,
                                       reverse_origin_address, select_place_candidate)
from oasis.core.data.source_status import inspect_data_sources
from oasis.core.data.official_resources import discover_official_resources
from oasis.core.cache import scoped_cache
from oasis.core.spatial.routes import plan_point_to_nearest_site
from oasis.services.run_store import RunStore


@dataclass
class _RunControl:
    """Keep non-persistable cancellation state for one live run."""
    token: CancellationToken
    future: Future | None = None
    terminal_status: str = "cancelled"
    process: multiprocessing.Process | None = None
    termination_lock: Lock = field(default_factory=Lock)
    llm_config: dict[str, Any] | None = None


def _run_process(
    cfg: dict, run_id: str, task_data: dict, spec_data: dict
) -> None:
    """Execute one run in a killable child process that writes durable state directly."""
    if os.name != "nt":
        os.setsid()
    service = OasisService(cfg=cfg, process_isolation=False, worker_mode=True)
    service.controls[run_id] = _RunControl(CancellationToken())
    service._run_serial(run_id, TaskSpec(**task_data), RunSpec(**spec_data))


class OasisService:
    """Own parsing, place resolution, jobs, backend files, and agent credentials."""

    def __init__(
        self,
        cfg: dict | None = None,
        llm_factory=None,
        max_workers: int = 2,
        *,
        process_isolation: bool | None = None,
        worker_mode: bool = False,
    ):
        """Create a service whose production runs execute in killable child processes."""
        self.cfg = cfg or load_cfg(); self.profiles = load_profiles()
        self.llm_factory = llm_factory or (lambda: LLMClient(self.cfg["llm"]))
        self.process_isolation = (
            llm_factory is None if process_isolation is None else process_isolation
        )
        self.executor = (
            None if worker_mode
            else ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="oasis")
        )
        self.lock = Lock()
        self.execution_lock = Lock()
        self.resolutions: dict[str, dict] = {}; self.uploads: dict[str, Path] = {}
        self.llm_sessions: dict[str, dict[str, Any]] = {}
        self.upload_root = PROJECT_ROOT / "tmp" / "uploads"; self.upload_root.mkdir(parents=True, exist_ok=True)
        for path in self.upload_root.glob("*.csv"):
            upload_id = path.name.split("_", 1)[0]
            if len(upload_id) == 32 and all(char in "0123456789abcdef" for char in upload_id.lower()):
                self.uploads[upload_id] = path
        output_root = Path(self.cfg["output"]["dir"])
        state_path = Path(self.cfg.get("runtime", {}).get("run_db", output_root / "runs.sqlite"))
        self.store = RunStore(state_path)
        if not worker_mode:
            self.store.interrupt_active()
        self.controls: dict[str, _RunControl] = {}
        self.progress_clocks: dict[str, tuple[str, float, int, str]] = {}

    def data_source_status(self, force: bool = False) -> dict[str, Any]:
        """Return a cached, read-only check of approved external sources."""
        return inspect_data_sources(self.cfg, force=force)

    def official_resources(self, place_name: str) -> dict[str, Any]:
        """Return notify-only city/county official catalog matches."""
        return discover_official_resources(place_name)

    def _llm_session_config(self, session_id: str | None) -> dict[str, Any] | None:
        if not session_id:
            return None
        with self.lock:
            session = self.llm_sessions.get(session_id)
            if not session or session["expires_at"] <= monotonic():
                self.llm_sessions.pop(session_id, None)
                raise KeyError("The LLM session is missing or expired. Test the connection again.")
            return deepcopy(session["config"])

    def revoke_llm_session(self, session_id: str) -> bool:
        """Remove one visitor model credential from process memory immediately."""
        with self.lock:
            return self.llm_sessions.pop(session_id, None) is not None

    def _validated_llm_url(self, provider: str, base_url: str, allow_private: bool) -> str:
        presets = {
            "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "openai": "https://api.openai.com/v1",
            "openrouter": "https://openrouter.ai/api/v1",
        }
        if provider in presets and base_url.rstrip("/") == presets[provider].rstrip("/"):
            return presets[provider]
        parsed = urlparse(base_url)
        if parsed.scheme not in ({"http", "https"} if provider == "local" else {"https"}) or not parsed.hostname:
            raise ValueError("Custom providers require an HTTPS base URL; local providers may use HTTP.")
        allowed_hosts = {str(value).lower() for value in self.cfg.get("llm", {}).get("allowed_custom_hosts", [])}
        if provider != "local" and not allow_private and parsed.hostname.lower() not in allowed_hosts:
            raise ValueError("This endpoint is not approved by the server administrator. Use the provider default or ask the administrator to allow its host.")
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))}
        except OSError as exc:
            raise ValueError("The model endpoint hostname could not be resolved.") from exc
        private = any(ipaddress.ip_address(value).is_private or ipaddress.ip_address(value).is_loopback
                      or ipaddress.ip_address(value).is_link_local for value in addresses)
        if private and not (provider == "local" and allow_private):
            raise ValueError("Private or loopback model endpoints are allowed only from a local OASIS session.")
        if provider == "local" and not private:
            raise ValueError("The local-model option must resolve to this machine or its private network.")
        return base_url.rstrip("/")

    def test_llm_connection(
        self, provider: str, base_url: str, model: str, api_key: str,
        *, allow_private: bool = False,
    ) -> dict[str, Any]:
        """Validate one provider and retain its secret only in process memory."""
        provider = provider.lower()
        if provider not in {"gemini", "openai", "openrouter", "custom", "local"}:
            raise ValueError("Choose Gemini, OpenAI, OpenRouter, custom HTTPS, or a local model.")
        if not model.strip():
            raise ValueError("Model is required.")
        if provider != "local" and not api_key.strip():
            raise ValueError("API key is required.")
        endpoint = self._validated_llm_url(provider, base_url, allow_private)
        runtime_config = {
            **self.cfg.get("llm", {}),
            "provider": provider,
            "base_url": endpoint,
            "model": model.strip(),
            "temperature": 0,
            "_session_api_key": api_key.strip() or "local-model",
        }
        probe_config = {
            **runtime_config,
            "max_tokens": min(
                int(runtime_config.get("max_tokens", 4096)),
                int(runtime_config.get("connection_test_max_tokens", 2048)),
            ),
        }
        started = monotonic()
        try:
            client = LLMClient(probe_config)
            if provider == "local":
                installed = client.list_models()
                aliases = {item.removesuffix(":latest") for item in installed}
                if model.strip() not in installed and model.strip() not in aliases:
                    available = ", ".join(installed) if installed else "none"
                    raise RuntimeError(
                        f"Local service is reachable, but model '{model.strip()}' is not installed. "
                        f"Run: ollama pull {model.strip()}. Installed models: {available}."
                    )
            system = "Parse a cooling-access request into a structured TaskSpec."
            user = (
                "Request: Select 2 cooling facilities in Phoenix within 15 minutes by walking. "
                "Return a TaskSpec with place_name Phoenix, Arizona, USA; analysis_goal "
                "facility_siting; n_facilities 2; threshold_minutes 15; and mobility_scenario walking."
            )
            probe_schema = TaskSpec.model_json_schema()
            compatibility_attempts = 0
            mismatches = []
            while compatibility_attempts < 2:
                compatibility_attempts += 1
                try:
                    response, resolved_model = client.probe(system, user, schema=probe_schema)
                except Exception as exc:
                    detail = str(exc).casefold()
                    terminal_markers = (
                        "401", "403", "invalid api key", "authentication", "unauthorized",
                        "permission denied", "insufficient quota", "user location is not supported",
                    )
                    if compatibility_attempts >= 2 or any(marker in detail for marker in terminal_markers):
                        raise
                    user += (
                        " The previous compatibility attempt did not return usable TaskSpec JSON. "
                        "Return only one TaskSpec JSON object with the exact requested values."
                    )
                    continue
                try:
                    parsed = TaskSpec.model_validate(response)
                except Exception as exc:
                    mismatches = [f"TaskSpec validation: {exc}"]
                else:
                    mismatches = []
                    if "phoenix" not in parsed.place_name.casefold():
                        mismatches.append(f"place_name={parsed.place_name or 'missing'}")
                    if parsed.analysis_goal != "facility_siting":
                        mismatches.append(f"analysis_goal={parsed.analysis_goal}")
                    if parsed.n_facilities != 2:
                        mismatches.append(f"n_facilities={parsed.n_facilities}")
                    if abs(parsed.threshold_minutes - 15.0) > 1e-6:
                        mismatches.append(f"threshold_minutes={parsed.threshold_minutes:g}")
                    if parsed.mobility_scenario != "walking":
                        mismatches.append(f"mobility_scenario={parsed.mobility_scenario}")
                if not mismatches:
                    break
                user += (
                    " Your previous JSON did not preserve the requested probe values. "
                    "Return the exact requested values in the TaskSpec."
                )
            if mismatches:
                detail = ", ".join(mismatches)[:240]
                raise RuntimeError(
                    "The endpoint accepted the credential and returned structured JSON, but the "
                    f"model did not follow the OASIS probe after two attempts ({detail})."
                )
        except Exception as exc:
            message = str(exc)
            if api_key: message = message.replace(api_key, "<redacted>")
            message = message[:400]
            raise RuntimeError(f"Model connection failed: {message}") from exc
        session_id = uuid4().hex
        ttl_seconds = 2 * 60 * 60
        with self.lock:
            now = monotonic()
            self.llm_sessions = {
                key: value for key, value in self.llm_sessions.items()
                if value["expires_at"] > now
            }
            if len(self.llm_sessions) >= 256:
                oldest = min(self.llm_sessions, key=lambda key: self.llm_sessions[key]["expires_at"])
                self.llm_sessions.pop(oldest, None)
            self.llm_sessions[session_id] = {
                "expires_at": now + ttl_seconds,
                "config": runtime_config,
            }
        return {
            "session_id": session_id,
            "provider": provider,
            "model": model.strip(),
            "resolved_model": resolved_model,
            "latency_ms": round((monotonic() - started) * 1000),
            "expires_in_seconds": ttl_seconds,
            "structured_output": isinstance(response, dict),
            "compatibility_attempts": compatibility_attempts,
        }

    def list_llm_models(
        self, provider: str, base_url: str, api_key: str, *, allow_private: bool = False,
    ) -> dict[str, Any]:
        """Discover models from an explicitly configured endpoint without retaining its key."""
        provider = provider.lower()
        if provider not in {"gemini", "openai", "openrouter", "custom", "local"}:
            raise ValueError("Choose Gemini, OpenAI, OpenRouter, custom HTTPS, or a local model.")
        if provider not in {"local", "openrouter"} and not api_key.strip():
            raise ValueError("Enter an API key before checking available models.")
        endpoint = self._validated_llm_url(provider, base_url, allow_private)
        if provider == "openrouter" and not api_key.strip():
            try:
                response = requests.get(
                    f"{endpoint}/models", timeout=20,
                    headers={"User-Agent": "OASIS model catalog/1.0"},
                )
                response.raise_for_status()
                models = sorted({
                    str(item["id"]) for item in response.json().get("data", [])
                    if isinstance(item, dict) and item.get("id") and (
                        "structured_outputs" in set(item.get("supported_parameters") or [])
                        or {"tools", "tool_choice"}.issubset(
                            set(item.get("supported_parameters") or [])
                        )
                    )
                })
            except Exception as exc:
                raise RuntimeError(
                    f"Could not read the public OpenRouter model list: {type(exc).__name__}."
                ) from exc
            return {"provider": provider, "models": models}
        config = {
            **self.cfg.get("llm", {}), "provider": provider, "base_url": endpoint,
            "model": "model-discovery", "_session_api_key": api_key.strip() or "local-model",
        }
        try:
            models = LLMClient(config).list_models()
        except Exception as exc:
            message = str(exc)
            if api_key: message = message.replace(api_key, "<redacted>")
            message = message[:400]
            raise RuntimeError(f"Could not read the endpoint model list: {message}") from exc
        return {"provider": provider, "models": models}

    def parse(self, prompt: str, llm_session_id: str | None = None) -> dict:
        """Parse a task draft while preserving missing-place HITL metadata."""
        scope = classify_request(prompt)
        if scope.disposition in {"needs_clarification", "out_of_scope"}:
            return {
                "disposition": scope.disposition, "task": None, "missing_place": False,
                "message": scope.alternative, "reason": scope.reason,
                "alternative": scope.alternative,
                "route_origin_candidates": [],
                "place_name_options": [],
                "events": [{"kind": "scope", "stage": "request routing",
                            "message": scope.reason, "data": {"disposition": scope.disposition}}],
            }
        session = self._llm_session_config(llm_session_id)
        llm = LLMClient(session) if session else self.llm_factory()
        orch = Orchestrator(llm, {**self.cfg, "value_profiles": self.profiles})
        message = ""; missing_place = False
        try: task = orch.parse_task(prompt).model_dump(mode="json")
        except MissingPlaceError as exc:
            task = TaskSpec(**{**exc.task_data, "place_name": ""}).model_dump(mode="json")
            message, missing_place = str(exc), True
        unsupported = [
            query for query in task.get("result_queries", []) if query.get("metric") == "unsupported"
        ]
        supported = [
            query for query in task.get("result_queries", []) if query.get("metric") != "unsupported"
        ]
        unsupported_notice = ""
        if unsupported and (supported or task.get("analysis_goal") != "tract_review"):
            labels = "; ".join(str(query.get("raw_text") or "unsupported property") for query in unsupported)
            task["result_queries"] = supported
            unsupported_notice = (
                "Some requested properties were omitted because the approved run data do not contain them: "
                f"{labels}"
            )
            task["notes"] = f"{str(task.get('notes') or '').strip()} [{unsupported_notice}]".strip()
        elif unsupported:
            question = unsupported[0].get("raw_text") or prompt
            return {
                "disposition": "out_of_scope", "task": None, "missing_place": False,
                "message": (
                    "OASIS does not have an approved tract-level indicator for this property. "
                    "Use an official city, county, or research dataset and add it through a reviewed data adapter before analysis."
                ),
                "reason": f"The requested property is outside the currently available data: {question}",
                "alternative": (
                    "Use an official source for the requested attribute, document its version and direction, "
                    "then add it to the audited preprocessing configuration."
                ),
                "route_origin_candidates": [],
                "place_name_options": [],
                "events": [event.model_dump(mode="json") for event in orch.ctx.history],
            }
        route_origin = task.get("route_origin")
        route_candidates = []
        route_origin_error = ""
        if route_origin:
            try:
                entity = route_origin["entity"]
                context = route_origin.get("context", "")
                positional_hint = route_origin.get("positional_hint", "")
                route_candidates, administrative_query = route_origin_options(
                    entity, context, positional_hint,
                    fallback_language=route_origin.get("source") == "fallback",
                )
                if not route_candidates:
                    raise ValueError(
                        "No address or landmark could be selected after exact and contextual "
                        "searches. Enter a street address, named landmark, intersection, postal "
                        "code, city, or county."
                    )
                if administrative_query:
                    task = TaskSpec(**{
                        **task, "place_name": ", ".join(
                            part for part in (entity, context) if part
                        ), "route_origin": None,
                    }).model_dump(mode="json")
                    route_candidates = []
                    message = (
                        f"{entity} was interpreted as the study area rather than an exact "
                        "route origin. Confirm its city or county boundary."
                    )
                else:
                    task = TaskSpec(**{**task, "place_name": ""}).model_dump(mode="json")
                    message = "Choose the intended route origin, then confirm its city or county study area."
                missing_place = False
            except ValueError as exc:
                task = TaskSpec(**{**task, "route_origin": None}).model_dump(mode="json")
                route_origin_error = str(exc)
                message = route_origin_error
                missing_place = not bool(str(task.get("place_name") or "").strip())
        if (not missing_place and task.get("analysis_goal") == "scenario_comparison"
                and len(task.get("comparison_profiles") or []) < 2):
            return {
                "disposition": "needs_clarification", "task": task, "missing_place": False,
                "message": "Select at least two registered value profiles in the comparison field below.",
                "reason": "A comparison needs two confirmed weighting schemes before it can run.",
                "alternative": "For an efficiency and inequality comparison, start with Efficiency first and Equity first.",
                "events": [event.model_dump(mode="json") for event in orch.ctx.history],
            }
        place_name_options = list(dict.fromkeys(
            str(value).strip() for value in task.get("place_names", []) if str(value).strip()
        ))
        if len(place_name_options) > 1:
            return {
                "disposition": "needs_clarification", "task": task, "missing_place": False,
                "message": "Choose one study area for this run.",
                "reason": (
                    "The request names multiple study areas. Each run keeps one boundary, data lineage, "
                    "travel matrix, and optimization result."
                ),
                "alternative": "Run the other area separately, then compare the saved runs.",
                "route_origin_candidates": route_candidates, "place_name_options": place_name_options,
                "events": [event.model_dump(mode="json") for event in orch.ctx.history],
            }
        ambiguities = [item for item in task.get("ambiguities", []) if item.get("field") != "place_names"]
        if ambiguities:
            questions = " ".join(str(item.get("question", "")).strip() for item in ambiguities).strip()
            return {
                "disposition": "needs_clarification", "task": task, "missing_place": False,
                "message": questions or "Confirm the ambiguous task fields before running.",
                "reason": "The language model identified uncertainty instead of silently guessing.",
                "alternative": questions or "Choose an option in the task confirmation panel.",
                "route_origin_candidates": route_candidates,
                "place_name_options": place_name_options,
                "events": [event.model_dump(mode="json") for event in orch.ctx.history],
            }
        disposition = "needs_clarification" if missing_place or route_candidates or route_origin_error else (
            "quick_diagnostic" if task.get("analysis_goal") in {
                "heat_review", "reachability_review", "tract_review",
                "minimum_facilities", "minimum_threshold",
            }
            else "full_analysis"
        )
        reason = ("Choose the intended point and confirm how it should be interpreted." if route_candidates else
                  "The route origin could not be resolved; the confirmed study area was preserved." if route_origin_error else
                  "A city or county is required before analysis." if missing_place else
                  "The named location is administrative, so it is being treated as the study area." if route_origin and not task.get("route_origin") else
                  "This reachability request prepares candidate facilities and then opens the existing point route planner." if route_origin else
                  "This request uses a bounded diagnostic workflow." if disposition == "quick_diagnostic" else
                  "This request uses the complete candidate facility analysis.")
        if unsupported_notice:
            reason = f"{reason} {unsupported_notice}"
        return {"disposition": disposition, "task": task, "missing_place": missing_place,
                "message": message, "reason": reason,
                "alternative": message if missing_place or route_origin_error else "",
                "route_origin_candidates": route_candidates,
                "place_name_options": [],
                "events": [event.model_dump(mode="json") for event in orch.ctx.history]}

    @staticmethod
    def _candidate(row: dict, index: int) -> dict:
        """Return compact public metadata for one retained geocoder candidate."""
        identity = f"{index}:{row.get('osm_type', 'x')}:{row.get('osm_id', 'x')}"
        canonical = canonical_place_name(row); kind = row.get("addresstype", "unknown")
        return {"candidate_id": identity, "canonical_name": canonical,
                "display_name": row.get("display_name", canonical), "place_type": kind,
                "label": f"{canonical} ({kind})"}

    def resolve_place(self, place_name: str) -> dict:
        """Resolve a place or return opaque candidates for explicit HITL."""
        try:
            selected, rows = select_place_candidate(place_name)
            ambiguous = False
        except AmbiguousPlaceError as exc:
            rows, selected, ambiguous = exc.candidates[:5], None, True
        resolution_id = uuid4().hex
        with self.lock: self.resolutions[resolution_id] = {"query": place_name, "rows": rows}
        candidates = [self._candidate(row, index) for index, row in enumerate(rows)]
        chosen = self._candidate(selected, rows.index(selected)) if selected else None
        return {"resolution_id": resolution_id, "query": place_name, "ambiguous": ambiguous,
                "candidates": candidates, "selected": chosen}

    def confirm_place(self, resolution_id: str, candidate_id: str) -> dict:
        """Confirm one retained candidate and write its canonical query cache."""
        with self.lock: resolution = self.resolutions.get(resolution_id)
        if resolution is None: raise KeyError("Unknown or expired place resolution")
        rows = resolution["rows"]
        try: index = int(candidate_id.split(":", 1)[0]); selected = rows[index]
        except (ValueError, IndexError): raise KeyError("Unknown place candidate") from None
        canonical = canonical_place_name(selected); cache_place_candidates(resolution["query"], rows, selected)
        return {**self._candidate(selected, index), "query": resolution["query"],
                "canonical_name": canonical}

    def reverse_route_origin(self, longitude: float, latitude: float) -> dict:
        """Resolve one exact browser coordinate into supported parent boundaries."""
        address = reverse_origin_address(latitude, longitude)
        coordinate = f"{latitude:.5f}, {longitude:.5f}"
        if str(address.get("country_code", "")).casefold() != "us":
            return {
                "outside_us": True,
                "message": (
                    f"Location received ({coordinate}), but it is outside the United States. "
                    "Current-location routing is available only for US study areas."
                ),
                "candidates": [],
            }
        state = address.get("state")
        if not state:
            return {
                "outside_us": False,
                "message": f"Location received ({coordinate}), but its state could not be identified.",
                "candidates": [],
            }
        candidates, seen = [], set()
        for key in ("city", "town", "municipality", "village", "county"):
            parent = address.get(key)
            if not parent:
                continue
            place_name = f"{parent}, {state}, USA"
            if place_name.casefold() in seen:
                continue
            seen.add(place_name.casefold())
            candidates.append({
                "candidate_id": hashlib.sha256(
                    f"{longitude:.6f}:{latitude:.6f}:{place_name}".encode()
                ).hexdigest()[:16],
                "display_name": f"Current location ({coordinate})",
                "place_name": place_name,
                "place_type": key,
                "entity": f"Current location ({coordinate})",
                "context": place_name,
                "positional_hint": "",
                "administrative": False,
                "longitude": float(longitude),
                "latitude": float(latitude),
            })
        if not candidates:
            return {
                "outside_us": False,
                "message": (
                    f"Location received ({coordinate}), but no supported city or county "
                    "boundary could be identified."
                ),
                "candidates": [],
            }
        return {
            "outside_us": False,
            "message": (
                "Location found. Choose the smallest applicable city, town, village, "
                "or county boundary."
            ),
            "candidates": candidates,
        }

    def search_route_origin(self, query: str) -> dict:
        """Search a manually refined origin without invoking task classification again."""
        choices, _ = route_origin_options(query.strip(), fallback_language=True)
        candidates = []
        for choice in choices:
            if choice.get("longitude") is None or choice.get("latitude") is None:
                continue
            candidate = dict(choice)
            # The user entered this value in the route-origin field. Even an
            # administrative match therefore means its representative point,
            # not a request to replace the task with an area-level diagnostic.
            candidate["administrative"] = False
            candidates.append(candidate)
        return {
            "outside_us": False,
            "message": (
                "Choose the intended route origin. Administrative matches use their "
                "representative map coordinate until a more exact address is supplied."
                if candidates else
                "No US address or landmark matched this route-origin search."
            ),
            "candidates": candidates,
        }

    def upload_candidates(self, filename: str, content: bytes) -> dict:
        """Store one CSV under an opaque ID and never expose its backend path."""
        name = Path(filename).name
        if Path(name).suffix.lower() != ".csv": raise ValueError("Candidate upload must be a CSV")
        upload_id = uuid4().hex; path = self.upload_root / f"{upload_id}_{name}"
        path.write_bytes(content)
        with self.lock: self.uploads[upload_id] = path
        return {"upload_id": upload_id, "filename": name, "size": len(content)}

    @staticmethod
    def _mode(value: str | None) -> RoutingMode:
        """Normalize legacy router names into the explicit execution modes."""
        normalized = {None: "auto", "ors": "precise", "haversine": "quick"}.get(value, value)
        if normalized not in {"quick", "precise", "auto"}:
            raise ValueError(f"Unsupported run mode: {value}")
        return normalized  # type: ignore[return-value]

    def _internal_task(self, task: TaskSpec, candidate_upload_id: str | None) -> TaskSpec:
        """Bind an opaque upload to an internal path without persisting that path publicly."""
        data = {**task.model_dump(), "candidate_file": None}
        if candidate_upload_id:
            with self.lock:
                candidate_file = self.uploads.get(candidate_upload_id)
            if candidate_file is None:
                raise KeyError("Unknown candidate upload")
            data.update(candidate_source="csv", candidate_file=str(candidate_file))
        return TaskSpec(**data)

    def _queue_run(
        self,
        spec: RunSpec,
        candidate_upload_id: str | None,
        *,
        parent_run_id: str | None = None,
        revision: int = 0,
        llm_session_id: str | None = None,
    ) -> dict:
        """Persist and submit one immutable run specification."""
        internal_task = self._internal_task(spec.task, candidate_upload_id)
        run_id = uuid4().hex
        public_spec = spec.model_dump(mode="json")
        public_spec["spec_hash"] = hashlib.sha256(
            json.dumps(public_spec, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.store.create(run_id, public_spec, parent_run_id=parent_run_id,
                          revision=revision, candidate_upload_id=candidate_upload_id)
        control = _RunControl(CancellationToken(), llm_config=self._llm_session_config(llm_session_id))
        with self.lock:
            self.controls[run_id] = control
        if self.executor is None:
            raise RuntimeError("Worker services cannot queue nested runs")
        control.future = self.executor.submit(self._run, run_id, internal_task, spec)
        return {"run_id": run_id, "status": "queued", "mode": spec.mode,
                "parent_run_id": parent_run_id, "revision": revision}

    def start_run(
        self,
        task: TaskSpec,
        mode: str = "auto",
        candidate_upload_id: str | None = None,
        *,
        router: str | None = None,
        llm_session_id: str | None = None,
    ) -> dict:
        """Queue one confirmed run; legacy router values remain accepted."""
        resolved = self._mode(router or mode)
        public_task = TaskSpec(**{**task.model_dump(), "candidate_file": None})
        return self._queue_run(RunSpec(task=public_task, mode=resolved), candidate_upload_id,
                               llm_session_id=llm_session_id)

    @staticmethod
    def _reuse_plan(old: RunSpec, new: RunSpec) -> dict[str, list[str]]:
        """Explain which content-addressed stages a revision can reuse."""
        old_task, new_task = old.task.model_dump(), new.task.model_dump()
        changed = {name for name in old_task if old_task[name] != new_task[name]}
        optimization_only = {"n_facilities", "weights", "facility_capacity", "coverage_target",
                             "coverage_group", "equity_alpha", "regional_requirements",
                             "preference_question", "notes"}
        if old.mode == new.mode and changed <= optimization_only:
            return {"reuse": ["study", "demand", "candidates", "travel_matrix"],
                    "recompute": ["optimization", "outputs"]}
        if old.mode == new.mode and changed <= optimization_only | {"threshold_minutes"}:
            return {"reuse": ["study", "demand", "candidates", "route_pairs"],
                    "recompute": ["travel_matrix", "optimization", "outputs"]}
        if old.task.place_name == new.task.place_name:
            return {"reuse": ["study", "provider_caches"],
                    "recompute": ["demand", "candidates", "travel_matrix", "optimization", "outputs"]}
        return {"reuse": ["provider_caches"],
                "recompute": ["study", "demand", "candidates", "travel_matrix", "optimization", "outputs"]}

    def revise_run(
        self,
        run_id: str,
        task: TaskSpec | None = None,
        mode: str | None = None,
        candidate_upload_id: str | None = None,
        llm_session_id: str | None = None,
    ) -> dict:
        """Create an immutable child run and cooperatively supersede active work."""
        old_record = self.store.get(run_id)
        old_spec = RunSpec(**old_record["spec"])
        new_task = task or old_spec.task
        new_mode = self._mode(mode or old_spec.mode)
        upload_id = candidate_upload_id or old_record["candidate_upload_id"]
        if new_task.candidate_source != "csv":
            upload_id = None
        new_spec = RunSpec(task=new_task, mode=new_mode)
        plan = self._reuse_plan(old_spec, new_spec)
        self._internal_task(new_task, upload_id)
        self.cancel_run(run_id, superseded=True)
        accepted = self._queue_run(new_spec, upload_id, parent_run_id=run_id,
                                   revision=int(old_record["revision"]) + 1,
                                   llm_session_id=llm_session_id)
        return {**accepted, **plan}

    def cancel_run(self, run_id: str, *, superseded: bool = False) -> dict:
        """Cancel immediately by terminating the isolated run process and its children."""
        record = self.store.get(run_id)
        terminal = {"cancelled", "superseded", "interrupted", "succeeded", "awaiting_hitl", "failed"}
        if record["status"] in terminal:
            return self.store.snapshot(run_id)
        desired = "superseded" if superseded else "cancelled"
        with self.lock:
            control = self.controls.get(run_id)
            if control:
                control.terminal_status = desired
                control.token.cancel()
                process = control.process
            else:
                process = None
        if record["status"] == "queued" and control and control.future and control.future.cancel():
            self.store.transition(run_id, ("queued",), desired, progress=None,
                                  cancel_reason="superseded" if superseded else "user")
        elif process is None:
            # Close the narrow window after a worker claims the future but before
            # an isolated process is registered. Late worker writes cannot replace
            # this terminal state because every transition is state-guarded.
            self.store.transition(
                run_id, ("queued", "running", "cancelling"), desired,
                progress=None, error=None,
                cancel_reason="superseded" if superseded else "user",
            )
        else:
            self.store.transition(run_id, ("queued", "running"), "cancelling",
                                  cancel_reason="superseded" if superseded else "user")
            if process is not None and process.is_alive():
                with control.termination_lock:
                    self._terminate_process(process)
                self.store.transition(
                    run_id, ("queued", "running", "cancelling"), desired,
                    progress=None, error=None,
                )
        return self.store.snapshot(run_id)

    @staticmethod
    def _terminate_process(process: multiprocessing.Process) -> None:
        """Terminate one run process tree and wait briefly for OS reclamation."""
        if process.pid is None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, check=False, timeout=5,
            )
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        process.join(timeout=2)
        if process.is_alive():
            process.kill()
            process.join(timeout=2)

    @staticmethod
    def _public(value: Any, key: str = "") -> Any:
        """Remove backend-managed filesystem paths from public payloads and events."""
        if key in {"candidate_file", "output_dir", "map", "pareto", "report", "analysis_chart"} and isinstance(value, str):
            return "<server-managed>"
        if isinstance(value, dict): return {name: OasisService._public(item, name) for name, item in value.items()}
        if isinstance(value, list): return [OasisService._public(item) for item in value]
        return value
    def _event(self, run_id: str, event: AgentEvent) -> None:
        """Append one ordered, durable public agent event."""
        payload = self._public({"kind": "agent", **event.model_dump(mode="json")})
        self.store.append_event(run_id, payload)

    def _progress(self, run_id: str, label: str, done: int, total: int, **details: Any) -> None:
        """Persist structured progress with a lightweight stage ETA."""
        now, done, total = monotonic(), int(done), int(total)
        activity_at = datetime.now(timezone.utc).isoformat()
        with self.lock:
            prior = self.progress_clocks.get(run_id)
            if prior is None or prior[0] != label or done < prior[2]:
                prior = (label, now, done, activity_at)
            self.progress_clocks[run_id] = prior
        elapsed = max(now - prior[1], 0.0)
        completed = max(done - prior[2], 0)
        rate = completed / elapsed if completed and elapsed else 0.0
        eta = max(total - done, 0) / rate if rate else (0.0 if done >= total else None)
        progress = {
            "kind": "progress", "label": label, "done": done, "total": total,
            "unit": details.pop("unit", "items"),
            "fraction": min(done / max(total, 1), 1.0),
            "eta_seconds": round(eta, 1) if eta is not None else None,
            "cache_hits": int(details.pop("cache_hits", details.pop("cache_pairs", 0)) or 0),
            "remote_pairs": int(details.pop("remote_pairs", details.pop("live_pairs", 0)) or 0),
            "inflight_pairs": int(details.pop("inflight_pairs", 0) or 0),
            "retry_after_seconds": details.pop("retry_after_seconds", None),
            "stage_started_at": prior[3],
            "activity_at": activity_at,
            "heartbeat_at": activity_at,
            "stage_elapsed_seconds": round(elapsed, 1),
            "last_activity_seconds": 0.0,
            **details,
        }
        self.store.update(run_id, progress=progress)

    @staticmethod
    def _metrics(orch: Orchestrator, payload: dict) -> dict:
        """Return compact presentation metrics without report-text scraping."""
        metrics = {}
        if orch.ctx.has("analysis"):
            solution = orch.ctx.get("analysis", {}).get("solution")
            if solution and solution.solver_status in {"Optimal", "Feasible"}:
                metrics = {"coverage": solution.covered_ratio,
                           "vulnerable": solution.vulnerable_covered_ratio,
                           "population_weighted_access_gini": solution.population_weighted_access_gini,
                           "mean_access_score": solution.mean_access_score,
                           "gradual_coverage": solution.gradual_coverage_ratio,
                           "assigned_population": solution.assigned_population,
                           "maximum_capacity_utilization": solution.maximum_capacity_utilization,
                           "avg_walk": solution.mean_travel_minutes}
        elif orch.ctx.has("sensitivity_results"):
            baseline = orch.ctx.get("sensitivity_results", {}).get("baseline", {})
            metrics = {"coverage": baseline.get("covered_population_ratio"),
                       "vulnerable": baseline.get("vulnerable_covered_ratio"),
                       "population_weighted_access_gini": baseline.get("population_weighted_access_gini"),
                       "mean_access_score": baseline.get("mean_access_score"),
                       "avg_walk": baseline.get("mean_travel_minutes")}
        for item in payload.get("results", []):
            if item.get("tool") == "build_travel_matrix" and item.get("ok"):
                routing = dict(item.get("summary", {})); provenance = item.get("provenance") or {}
                routing["tier"] = provenance.get("tier", "unknown"); payload["routing_status"] = routing
        return metrics

    @staticmethod
    def _add_artifact(manifest: dict[str, Path], name: str, value: str | None,
                      allowed_root: Path) -> None:
        """Allow-list one existing artifact contained by its run directory."""
        if not value: return
        path = Path(value).resolve()
        if path.is_file() and path.is_relative_to(allowed_root.resolve()): manifest[name] = path

    def _artifacts(self, run_id: str, payload: dict, root: Path) -> dict[str, Path]:
        """Collect output, heat, and diagnostic artifacts under stable public names."""
        manifest: dict[str, Path] = {}; outputs = payload.get("outputs") or {}
        for name in ("map", "pareto", "report"):
            self._add_artifact(manifest, name, outputs.get(name), root)
        heat = payload.get("heat_summary") or {}
        if "map" not in manifest: self._add_artifact(manifest, "map", heat.get("map"), root)
        self._add_artifact(manifest, "heat_analysis", heat.get("analysis_chart"), root)
        if "report" not in manifest: self._add_artifact(manifest, "report", heat.get("report"), root)
        diagnostics = payload.get("diagnostics") or {}
        for handle in ("minimum_facilities", "minimum_threshold", "reachability_analysis",
                       "scenario_comparison", "result_query_answers"):
            if "map" not in manifest:
                diagnostic = diagnostics.get(handle) or {}
                map_path = diagnostic.get("map")
                if handle == "result_query_answers":
                    answers = diagnostic.get("answers") or []
                    map_path = answers[0].get("map") if answers else None
                self._add_artifact(manifest, "map", map_path, root)
        if "report" not in manifest and (payload.get("recovery") or diagnostics):
            report = root / "feasibility_diagnostic.md"; report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(render_diagnostic_report(payload.get("recovery"), diagnostics), encoding="utf-8")
            manifest["report"] = report
        return manifest

    def _phase_config(self, phase: str, root: Path) -> dict:
        """Return isolated configuration for one quick or precise phase."""
        precise = phase == "precise"
        cfg = deepcopy(self.cfg)
        cfg["output"] = {**cfg.get("output", {}), "dir": str(root)}
        cfg["accessibility"] = {
            **cfg.get("accessibility", {}),
            "router": "ors" if precise else "haversine",
            "routing_mode": phase,
            "strict_router": precise,
            "strict_routing": precise,
            "allow_fallback": not precise,
        }
        cfg["data"] = deepcopy(cfg.get("data", {}))
        cfg["data"]["landsat_environment_indices"] = {
            **cfg["data"].get("landsat_environment_indices", {}),
            # Keep the approximate preview fast; the precise phase computes
            # and caches diagnostic optical indices with visible progress.
            "enabled": precise,
        }
        cfg["optimization"] = deepcopy(cfg.get("optimization", {}))
        if not precise:
            # A preview must become useful quickly, especially for large metros.
            # The precise phase retains the configured full sweep limits.
            cfg["optimization"]["solver_time_limit_s"] = min(
                cfg["optimization"].get("solver_time_limit_s", 120), 30
            )
            cfg["optimization"]["minimum_threshold_total_time_limit_s"] = min(
                cfg["optimization"].get("minimum_threshold_total_time_limit_s", 90),
                cfg["optimization"].get("minimum_threshold_preview_time_limit_s", 25),
            )
            cfg["optimization"]["pareto_point_time_limit_s"] = min(
                cfg["optimization"].get("pareto_point_time_limit_s", 30), 10
            )
            cfg["optimization"]["pareto_total_time_limit_s"] = min(
                cfg["optimization"].get("pareto_total_time_limit_s", 90), 25
            )
        cfg["value_profiles"] = self.profiles
        cfg["max_self_correction_rounds"] = cfg.get("llm", {}).get("max_self_correction_rounds", 3)
        return cfg

    def _publish_artifacts(
        self, run_id: str, phase: str, manifest: dict[str, Path], *, preview: bool
    ) -> dict[str, str]:
        """Register private paths and return phase-stable public URLs."""
        stored = {(f"preview_{name}" if preview else name): path for name, path in manifest.items()}
        self.store.put_artifacts(run_id, phase, stored)
        return {name: f"/v1/runs/{run_id}/artifacts/"
                      f"{'preview_' if preview else ''}{name}" for name in manifest}

    @staticmethod
    def _carry_preview_diagnostic_bounds(
        task: TaskSpec, prior_preview: dict | None, payload: dict
    ) -> None:
        """Keep an explicitly labeled screening bound that sparse ORS cannot recompute."""
        if task.analysis_goal != "minimum_facilities" or not prior_preview:
            return
        precise_diagnostics = payload.setdefault("diagnostics", {})
        precise_facilities = precise_diagnostics.get("minimum_facilities") or {}
        precise_threshold = precise_diagnostics.get("minimum_threshold") or {}
        preview_threshold = (prior_preview.get("diagnostics") or {}).get("minimum_threshold") or {}
        if (
            precise_facilities.get("feasible")
            or precise_threshold.get("feasible")
            or not preview_threshold.get("feasible")
        ):
            return
        screening = deepcopy(preview_threshold)
        screening["accuracy"] = "approximate_haversine"
        screening["interpretation"] = (
            "Screening estimate retained from the approximate phase because the ORS "
            "matrix is sparse outside the confirmed travel threshold."
        )
        precise_diagnostics["approximate_minimum_threshold"] = screening
        prefix = str(payload.get("message") or "").strip()
        preview_facilities = (prior_preview.get("diagnostics") or {}).get("minimum_facilities") or {}
        coverage_target = float(preview_facilities.get("coverage_target", 1.0))
        coverage_group = str(preview_facilities.get("coverage_group", "all")).replace("_", " ")
        confirmed_threshold = float(preview_facilities.get("threshold_minutes", task.threshold_minutes))
        minutes = float(screening["minimum_threshold_minutes"])
        facilities = int(screening.get("n_facilities", task.n_facilities))
        preview_message = (
            f"No number of the retained candidate facilities can cover {coverage_target:.0%} "
            f"of the {coverage_group} group within {confirmed_threshold:g} minutes. With "
            f"{facilities} facilities, the smallest computed threshold is {minutes:.1f} "
            f"minutes by {task.mobility_scenario.replace('_', ' ')}."
        )
        supplement = " The approximate Haversine screening result is retained"
        supplement += f": {preview_message}"
        payload["message"] = prefix + supplement

    def _execute_phase(
        self, run_id: str, task: TaskSpec, spec: RunSpec, phase: str, *, preview: bool,
        prior_preview: dict | None = None,
    ) -> tuple[str, dict]:
        """Execute one auditable phase and return its terminal payload."""
        root = Path(self.cfg["output"]["dir"]) / "api_runs" / run_id / phase
        cfg = self._phase_config(phase, root)
        with self.lock:
            control = self.controls[run_id]
            self.progress_clocks.pop(run_id, None)
        llm = LLMClient(control.llm_config) if control.llm_config else self.llm_factory()
        orch = Orchestrator(llm, cfg)
        orch.ctx.cancel_callback = control.token.is_cancelled
        orch.ctx.event_callback = lambda event: self._event(run_id, event)
        orch.ctx.progress_callback = (
            lambda label, done, total, **stats: self._progress(run_id, label, done, total, **stats)
        )
        orch.ctx.record("hitl", "Human-confirmed task parameters were bound before execution.",
                        place_name=task.place_name, n_facilities=task.n_facilities,
                        threshold_minutes=task.threshold_minutes,
                        weights=task.weights.model_dump() if task.weights else None,
                        candidate_source=task.candidate_source, mode=spec.mode, phase=phase,
                        mobility_scenario=task.mobility_scenario,
                        operating_window=(task.operating_window.model_dump(mode="json")
                                          if task.operating_window else None))
        internal = jsonable_encoder(orch.run(task))
        analysis = orch.ctx.get("analysis", {}) if orch.ctx.has("analysis") else {}
        frontier_alphas = {round(point.alpha, 8) for point in analysis.get("frontier", [])}
        candidate_lookup = {}
        if orch.ctx.has("candidates"):
            for _, row in orch.ctx.get("candidates").to_crs("EPSG:4326").iterrows():
                geometry = row.geometry
                if geometry is not None and not geometry.is_empty:
                    point = geometry if geometry.geom_type == "Point" else geometry.representative_point()
                    candidate_lookup[str(row["cand_id"])] = {
                        "id": str(row["cand_id"]),
                        "name": str(row.get("name") or row["cand_id"]),
                        "longitude": float(point.x), "latitude": float(point.y),
                    }
        internal["pareto_points"] = [
            {
                "alpha": point.alpha, "efficiency": point.efficiency_metric,
                "equality": point.equity_metric, "gini": point.population_weighted_gini,
                "covered_population_ratio": point.solution.covered_ratio,
                "vulnerable_coverage_ratio": point.solution.vulnerable_covered_ratio,
                "mean_access_score": point.solution.mean_access_score,
                "mean_travel_minutes": point.solution.mean_travel_minutes,
                "selected_ids": point.solution.selected_ids,
                "selected_sites": [candidate_lookup[value] for value in map(str, point.solution.selected_ids)
                                   if value in candidate_lookup],
                "is_frontier": round(point.alpha, 8) in frontier_alphas,
            }
            for point in analysis.get("points", [])
        ]
        internal["task"] = spec.task.model_dump(mode="json")
        if phase == "precise":
            self._carry_preview_diagnostic_bounds(task, prior_preview, internal)
        internal["metrics"] = self._metrics(orch, internal)
        routing = internal.get("routing_status") or {}
        router_used = routing.get("router")
        if phase == "precise" and routing and not str(router_used).startswith("ors"):
            raise RuntimeError(f"Precise mode requires ORS network routing; got {router_used or 'fallback'}")
        cache_keys = ("matrix_cache_hit", "cache_pairs", "live_pairs", "eligible_pairs",
                      "pruned_pairs", "planned_requests", "completed_requests")
        execution = ExecutionProvenance(
            mode=spec.mode, phase=phase,
            router_requested="ors" if phase == "precise" else "haversine",
            router_used=router_used,
            accuracy=("network" if phase == "precise" else "approximate") if routing else None,
            status="complete" if internal.get("ok") else "failed",
            parameters={"mobility_scenario": task.mobility_scenario,
                        "modes": routing.get("modes", [task.mobility_scenario]),
                        "operating_window": (task.operating_window.model_dump(mode="json")
                                             if task.operating_window else None),
                        "mode_population_share": routing.get("mode_population_share", {}),
                        "operating_hours": routing.get("operating_hours", {}),
                        "threshold_minutes": task.threshold_minutes,
                        "strict": phase == "precise"},
            cache_stats={key: routing[key] for key in cache_keys if key in routing},
        )
        internal["execution"] = execution.model_dump(mode="json")
        manifest = self._artifacts(run_id, internal, root)
        report = manifest.get("report")
        if report:
            spec_hash = self.store.get(run_id)["spec"].get("spec_hash", "unknown")
            section = (
                "\n\n## Execution Provenance\n\n"
                f"- Run specification hash: `{spec_hash}`\n"
                f"- Requested mode: {spec.mode}; completed phase: {phase}\n"
                f"- Router requested: {execution.router_requested}; router used: "
                f"{execution.router_used or 'not applicable'}\n"
                f"- Accuracy class: {execution.accuracy or 'not applicable'}\n"
                f"- Routing parameters: `{json.dumps(execution.parameters, sort_keys=True)}`\n"
                f"- Cache statistics: `{json.dumps(execution.cache_stats, sort_keys=True)}`\n"
            )
            notice = ""
            if execution.accuracy == "approximate":
                notice = (
                    "> **Approximate preview (Haversine):** This report uses estimated "
                    "straight-line travel times. Wait for ORS refinement before treating "
                    "the siting result as final.\n\n"
                )
            elif execution.accuracy == "network":
                notice = (
                    "**Precise network result (ORS):** Travel times use the completed "
                    "OpenRouteService network matrix.\n\n"
                )
            report.write_text(
                notice + report.read_text(encoding="utf-8").rstrip() + section,
                encoding="utf-8",
            )
        payload = self._public(internal)
        payload["task"] = spec.task.model_dump(mode="json")
        payload["artifacts"] = self._publish_artifacts(run_id, phase, manifest, preview=preview)
        if (
            phase == "precise" and prior_preview
            and "map" not in payload["artifacts"]
            and (prior_preview.get("artifacts") or {}).get("map")
        ):
            payload["artifacts"]["map"] = prior_preview["artifacts"]["map"]
            payload.setdefault("diagnostics", {}).setdefault(
                "approximate_map", {}
            ).update({
                "retained": True,
                "accuracy": "approximate_haversine",
                "interpretation": (
                    "The precise diagnostic produced no replacement map, so the "
                    "approximate phase map remains visible."
                ),
            })
        status = "succeeded" if payload["ok"] else "awaiting_hitl" if payload.get("recovery") else "failed"
        return status, payload

    def _retain_approximate(
        self, run_id: str, payload: dict, exc: Exception, *, preview: bool
    ) -> None:
        """Finish successfully with an honest warning when ORS cannot refine a result."""
        reason = str(exc)
        guard_note = (
            " This is an OASIS request-volume guard, not an error returned by ORS."
            if "planned ORS requests" in reason else ""
        )
        warning = (
            "The full study-area ORS matrix was unavailable, so the completed approximate "
            f"selection result has been retained. Reason: {reason}{guard_note} Point route "
            "planning remains available because it sends only a small shortlist to ORS. "
            "For a full ORS matrix, reduce the travel threshold, choose fewer candidate "
            "facility types, upload a smaller reviewed candidate set, or use a smaller "
            "study area. Change mobility only when that matches the policy question."
        )
        retained = deepcopy(payload)
        retained["message"] = warning
        diagnostics = dict(retained.get("diagnostics") or {})
        diagnostics["precise_refinement"] = {
            "status": "unavailable", "reason": str(exc),
            "retained_result": "approximate_haversine",
        }
        retained["diagnostics"] = diagnostics
        artifact_name = "preview_report" if preview else "report"
        try:
            report = self.store.artifact(run_id, artifact_name)
            old_notice = (
                "> **Approximate preview (Haversine):** This report uses estimated "
                "straight-line travel times. Wait for ORS refinement before treating "
                "the siting result as final.\n\n"
            )
            fallback_notice = (
                "> **Approximate fallback (Haversine):** The full study-area ORS matrix "
                "could not be completed, so the approximate selection result is retained. "
                f"Reason: {exc} Point route planning remains available through a small "
                "ORS shortlist.\n\n"
            )
            report.write_text(
                report.read_text(encoding="utf-8").replace(
                    old_notice, fallback_notice, 1
                ),
                encoding="utf-8",
            )
        except (KeyError, OSError):
            pass
        self.store.transition(
            run_id, ("running",), "succeeded",
            preview_result=retained if preview else None,
            result=None if preview else retained,
            error=str(exc), progress=None,
        )
        self.store.append_event(run_id, {
            "kind": "finish", "stage": "precise fallback", "message": warning,
            "data": {"accuracy": "approximate", "router": "haversine"},
        })

    def _fail_run(
        self, run_id: str, spec: RunSpec, phase: str, exc: Exception,
        task: TaskSpec | None = None,
    ) -> None:
        """Persist a failure, retaining or generating an approximate ORS fallback."""
        snapshot = self.store.snapshot(run_id)
        if spec.mode == "auto" and phase == "precise" and snapshot.get("preview_result"):
            self._retain_approximate(
                run_id, snapshot["preview_result"], exc, preview=True
            )
            return
        if phase == "precise" and task is not None and "Precise mode requires ORS" in str(exc):
            try:
                status, payload = self._execute_phase(
                    run_id, task, spec, "quick", preview=False
                )
            except Exception:
                status = "failed"
            if status == "succeeded":
                self._retain_approximate(run_id, payload, exc, preview=False)
                return
        recovery = {"diagnosis": str(exc),
                    "question": "Review the request and backend configuration before retrying.",
                    "suggested_changes": {}}
        payload = {"ok": False, "paused": True, "message": str(exc), "failed_tool": "backend",
                   "task": spec.task.model_dump(mode="json"), "results": [], "lineage": [],
                   "diagnostics": {}, "recovery": recovery, "metrics": {}, "artifacts": {},
                   "execution": ExecutionProvenance(
                       mode=spec.mode, phase=phase,
                       router_requested="ors" if phase == "precise" else "haversine",
                       status="failed").model_dump(mode="json")}
        root = Path(self.cfg["output"]["dir"]) / "api_runs" / run_id / phase
        root.mkdir(parents=True, exist_ok=True)
        report = root / "feasibility_diagnostic.md"
        report.write_text(render_diagnostic_report(recovery, {}), encoding="utf-8")
        preview = spec.mode == "auto" and phase == "quick"
        payload["artifacts"] = self._publish_artifacts(
            run_id, phase, {"report": report}, preview=preview
        )
        self.store.transition(run_id, ("running",), "failed", result=payload,
                              error=str(exc), progress=None)

    def _run(self, run_id: str, task: TaskSpec, spec: RunSpec) -> None:
        """Expose a cancellable wait state before serialized geospatial execution."""
        if not self.store.transition(run_id, ("queued",), "running"):
            return
        phase = "quick" if spec.mode in {"quick", "auto"} else "precise"
        self.store.update(
            run_id,
            phase=phase,
            progress={
                "kind": "progress",
                "label": "Waiting for the previous run to stop",
                "done": 0,
                "total": 1,
                "unit": "run",
                "fraction": 0.0,
                "eta_seconds": None,
                "cache_hits": 0,
                "remote_pairs": 0,
                "inflight_pairs": 0,
            },
        )
        control = self.controls[run_id]
        acquired = False
        heartbeat_interval = max(
            1.0, float(self.cfg.get("execution", {}).get("run_heartbeat_interval_s", 5))
        )
        next_heartbeat = monotonic() + heartbeat_interval
        try:
            while not acquired:
                control.token.check()
                acquired = self.execution_lock.acquire(timeout=0.05)
                if not acquired and monotonic() >= next_heartbeat:
                    self.store.heartbeat(run_id)
                    next_heartbeat = monotonic() + heartbeat_interval
            control.token.check()
            if self.process_isolation:
                self._run_isolated(run_id, task, spec, control)
            else:
                self._run_serial(run_id, task, spec)
        except RunCancelled:
            self.store.transition(
                run_id,
                ("running", "cancelling"),
                control.terminal_status,
                progress=None,
                error=None,
            )
        finally:
            if acquired:
                self.execution_lock.release()
            with self.lock:
                self.controls.pop(run_id, None)
                self.progress_clocks.pop(run_id, None)

    def _run_isolated(
        self, run_id: str, task: TaskSpec, spec: RunSpec, control: _RunControl
    ) -> None:
        """Start and monitor a whole-run subprocess so cancellation is stage-independent."""
        run_cfg = deepcopy(self.cfg)
        if control.llm_config:
            run_cfg["llm"] = deepcopy(control.llm_config)
        process = multiprocessing.get_context("spawn").Process(
            target=_run_process,
            args=(run_cfg, run_id, task.model_dump(mode="json"), spec.model_dump(mode="json")),
            name=f"oasis-run-{run_id[:8]}",
        )
        process.start()
        with self.lock:
            control.process = process
        heartbeat_interval = max(
            1.0, float(self.cfg.get("execution", {}).get("run_heartbeat_interval_s", 5))
        )
        next_heartbeat = monotonic() + heartbeat_interval
        try:
            while process.is_alive():
                if control.token.cancelled():
                    with control.termination_lock:
                        self._terminate_process(process)
                    raise RunCancelled()
                if monotonic() >= next_heartbeat:
                    self.store.heartbeat(run_id)
                    next_heartbeat = monotonic() + heartbeat_interval
                process.join(timeout=0.1)
            if process.exitcode:
                if control.token.cancelled():
                    raise RunCancelled()
                record = self.store.get(run_id)
                if record["status"] in {"queued", "running", "cancelling"}:
                    raise RuntimeError(
                        f"Run worker exited unexpectedly with code {process.exitcode}"
                    )
        finally:
            with self.lock:
                control.process = None

    def _run_serial(self, run_id: str, task: TaskSpec, spec: RunSpec) -> None:
        """Run one geospatial job while preserving process-wide single-flight safety."""
        phase = "quick" if spec.mode in {"quick", "auto"} else "precise"
        try:
            with self.lock:
                control = self.controls[run_id]
            phases = ("quick", "precise") if spec.mode == "auto" else (phase,)
            for phase in phases:
                control.token.check()
                self.store.update(run_id, phase=phase, progress=None)
                prior_preview = (
                    self.store.snapshot(run_id).get("preview_result")
                    if spec.mode == "auto" and phase == "precise"
                    else None
                )
                status, payload = self._execute_phase(
                    run_id, task, spec, phase,
                    preview=spec.mode == "auto" and phase == "quick",
                    prior_preview=prior_preview,
                )
                control.token.check()
                if phase == "precise" and status != "succeeded":
                    diagnosis = str((payload.get("recovery") or {}).get("diagnosis") or "")
                    if "Precise mode requires ORS" in diagnosis:
                        self._fail_run(run_id, spec, phase, RuntimeError(diagnosis), task)
                        return
                if spec.mode == "auto" and phase == "quick":
                    self.store.update(run_id, preview_result=payload, progress=None)
                    if status == "succeeded":
                        routing = payload.get("routing_status") or {}
                        reports = routing.get("mode_reports") or {}
                        eligible_pairs = max(
                            (int(report.get("eligible_pairs", report.get("total_pairs", 0)) or 0)
                             for report in reports.values()),
                            default=0,
                        )
                        pair_limit = int(
                            self.cfg.get("accessibility", {}).get(
                                "ors_max_eligible_pairs", 250000
                            )
                        )
                        if eligible_pairs > pair_limit:
                            self._fail_run(
                                run_id, spec, "precise",
                                RuntimeError(
                                    "Precise ORS refinement was skipped because the "
                                    f"approximate matrix contains {eligible_pairs:,} "
                                    "eligible demand-candidate pairs after distance pruning, "
                                    "above the automatic ORS "
                                    f"preflight limit of {pair_limit:,}."
                                ), task,
                            )
                            return
                        continue
                if not self.store.transition(run_id, ("running",), status,
                                             result=payload, progress=None):
                    control.token.check()
                return
        except RunCancelled:
            with self.lock:
                control = self.controls.get(run_id)
                desired = control.terminal_status if control else "cancelled"
            self.store.transition(run_id, ("queued", "running", "cancelling"), desired,
                                  progress=None, error=None)
        except Exception as exc:
            with self.lock:
                control = self.controls.get(run_id)
            if control and control.token.cancelled():
                self.store.transition(run_id, ("queued", "running", "cancelling"),
                                      control.terminal_status, progress=None, error=None)
            else:
                self._fail_run(run_id, spec, phase, exc, task)

    def run_snapshot(self, run_id: str) -> dict:
        """Return one durable JSON-safe copy of current job state."""
        return self.store.snapshot(run_id)

    def _visible_map_artifact(self, run_id: str) -> Path:
        """Return the precise map when present, otherwise the retained preview map."""
        for name in ("map", "preview_map"):
            try:
                path = self.store.artifact(run_id, name)
            except KeyError:
                continue
            if path.is_file():
                return path
        raise ValueError("point route planning requires an available map result")

    def plan_point_route(
        self, run_id: str, longitude: float, latitude: float, mode: str
    ) -> dict:
        """Plan a transient clicked-point route from the map currently visible to the user."""
        import geopandas as gpd
        from shapely.geometry import Point, shape
        from shapely.ops import unary_union

        snapshot = self.store.snapshot(run_id)
        map_path = self._visible_map_artifact(run_id)
        payload = json.loads(map_path.read_text(encoding="utf-8"))
        tract_features = [
            feature for feature in payload.get("features", [])
            if feature.get("properties", {}).get("layer_kind") == "tract"
        ]
        site_features = [
            feature for feature in payload.get("features", [])
            if feature.get("properties", {}).get("layer_kind") == "selected_site"
        ]
        boundary_features = [
            feature for feature in payload.get("features", [])
            if feature.get("properties", {}).get("layer_kind") == "boundary"
        ]
        if not tract_features or not site_features:
            raise ValueError("completed map does not contain tracts and selected candidate facilities")
        areas = gpd.GeoDataFrame.from_features(tract_features, crs="EPSG:4326")
        areas["mean_lst"] = areas.get("tract_mean_heat_c")
        candidates = gpd.GeoDataFrame.from_features(site_features, crs="EPSG:4326")
        candidates["cand_id"] = candidates["candidate_id"].astype(str)
        place_name = snapshot["task"]["place_name"]
        study_boundary = unary_union([shape(feature["geometry"]) for feature in boundary_features]) \
            if boundary_features else areas.geometry.union_all()
        origin = Point(float(longitude), float(latitude))
        if not study_boundary.covers(origin):
            message = (
                f"This location is outside the analyzed boundary for {place_name}. "
                "The origin is shown briefly before the map returns to the study area."
            )
            return {
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [float(longitude), float(latitude)]},
                    "properties": {"layer_kind": "route_origin", "outside_study_area": True},
                }],
                "summary": {
                    "status": "outside_study_area", "message": message,
                    "origin_label": "Selected origin", "origin_longitude": float(longitude),
                    "origin_latitude": float(latitude), "study_area": place_name,
                },
            }
        accessibility = self.cfg.get("accessibility", {})
        with scoped_cache(place_name):
            return plan_point_to_nearest_site(
                longitude, latitude, areas, candidates, candidates["cand_id"].tolist(),
                mode=mode, base_url=(
                    accessibility.get("ors_base_url")
                    or "https://api.heigit.org/openrouteservice/v2/matrix"
                ),
                timeout_s=accessibility.get("ors_timeout_s", 60),
                request_interval_s=accessibility.get("ors_request_interval_s", 1.6),
                candidate_limit=accessibility.get("point_route_candidate_limit", 15),
                study_boundary=study_boundary,
            )

    def plan_place_route(self, run_id: str, query: str, mode: str) -> dict:
        """Geocode one US origin inside the displayed study area and plan its route."""
        import geopandas as gpd
        from shapely.geometry import Point, shape
        from shapely.ops import unary_union

        snapshot = self.store.snapshot(run_id)
        payload = json.loads(
            self._visible_map_artifact(run_id).read_text(encoding="utf-8")
        )
        tract_features = [
            feature for feature in payload.get("features", [])
            if feature.get("properties", {}).get("layer_kind") == "tract"
        ]
        boundary_features = [
            feature for feature in payload.get("features", [])
            if feature.get("properties", {}).get("layer_kind") == "boundary"
        ]
        if not tract_features:
            raise ValueError("the displayed map contains no Census tract geometry")
        areas = gpd.GeoDataFrame.from_features(tract_features, crs="EPSG:4326")
        study_boundary = unary_union([shape(feature["geometry"]) for feature in boundary_features]) \
            if boundary_features else areas.geometry.union_all()
        place_name = snapshot["task"]["place_name"]
        with scoped_cache(place_name):
            rows = search_origin_candidates(query)
        if not rows:
            raise ValueError(f"Location not found in the United States: {query}")
        matched = None
        for row in rows:
            try:
                point = Point(float(row["lon"]), float(row["lat"]))
            except (KeyError, TypeError, ValueError):
                continue
            if study_boundary.covers(point):
                matched = (row, point)
                break
        if matched is None:
            row = rows[0]
            point = Point(float(row["lon"]), float(row["lat"]))
            first = row.get("display_name", query)
            message = (
                f"Location found ({first}), but it is outside the analyzed boundary for {place_name}. "
                "The origin is shown briefly before the map returns to the study area."
            )
            return {
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [float(point.x), float(point.y)]},
                    "properties": {"layer_kind": "route_origin", "outside_study_area": True},
                }],
                "summary": {
                    "status": "outside_study_area", "message": message,
                    "origin_query": query, "origin_label": first,
                    "origin_longitude": float(point.x), "origin_latitude": float(point.y),
                    "origin_geocoder": "OpenStreetMap Nominatim", "study_area": place_name,
                },
            }
        row, point = matched
        result = self.plan_point_route(run_id, float(point.x), float(point.y), mode)
        result["summary"].update({
            "origin_query": query,
            "origin_label": row.get("display_name", query),
            "origin_longitude": float(point.x),
            "origin_latitude": float(point.y),
            "origin_geocoder": "OpenStreetMap Nominatim",
        })
        return result

    def artifact(self, run_id: str, name: str) -> Path:
        """Return one explicitly allow-listed run artifact."""
        path = self.store.artifact(run_id, name)
        if not path.is_file():
            raise KeyError("Unknown artifact")
        return path
