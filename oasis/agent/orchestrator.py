"""LLM orchestration for structured parsing, planning, repair, and explanation."""
from __future__ import annotations
import json, logging, math, os, pathlib, re
from typing import Any
from oasis.agent.context import Context
from oasis.agent.scope import route_origin_from_prompt
from oasis.core.cancellation import RunCancelled
from oasis.contracts.domain import (AgentAction, AgentPlan, OperatingWindow, PlanStep, RecoveryProposal,
                           RegionalRequirement, ResultNarrative, RouteOriginSpec, TaskSpec,
                           ToolError, ToolResult, ValueWeights)
from oasis.services.registry import registry

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


def _slug(value: Any) -> str:
    """Normalize one provider enum token without interpreting user prose."""
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _normalize_result_queries(
    value: Any, user_prompt: str, adjustments: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Converge provider JSON variants into valid typed read-only queries."""
    if value in (None, ""):
        return []
    items = value if isinstance(value, list) else [value]
    normalized: list[dict[str, Any]] = []
    for item in items:
        query = {"metric": item} if isinstance(item, str) else dict(item) if isinstance(item, dict) else {}
        metric = _slug(query.get("metric"))
        metric_aliases = {
            "heat": "heat_exposure", "temperature": "heat_exposure",
            "composite_vulnerability": "vulnerability", "vulnerability_score": "vulnerability",
            "inequality": "access_disadvantage", "access_inequality": "access_disadvantage",
            "equity_access_gap": "combined_disadvantage",
            "vulnerability_access_gap": "combined_disadvantage",
            "combined_vulnerability_access_gap": "combined_disadvantage",
        }
        supported = {
            "heat_exposure", "vulnerability", "access_disadvantage",
            "combined_disadvantage", "coverage_gap",
        }
        metric = metric_aliases.get(metric, metric)
        query["metric"] = metric if metric in supported else "unsupported"
        direction = _slug(query.get("direction", "highest"))
        direction_aliases = {
            "greatest": "highest", "most": "highest", "maximum": "highest", "max": "highest",
            "desc": "highest", "descending": "highest", "largest": "highest", "higher": "highest", "top": "highest",
            "least": "lowest", "minimum": "lowest", "min": "lowest", "smallest": "lowest",
            "asc": "lowest", "ascending": "lowest", "lower": "lowest", "bottom": "lowest",
        }
        query["direction"] = direction_aliases.get(direction, direction)
        if query["direction"] not in {"highest", "lowest"}:
            query["direction"] = "highest"
            if adjustments is not None:
                adjustments.append(f"invalid result direction '{direction}' replaced with highest")
        try:
            raw_limit = float(query.get("limit", 5))
            if not math.isfinite(raw_limit) or not raw_limit.is_integer():
                raise ValueError
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 5
            if adjustments is not None:
                adjustments.append("invalid result limit replaced with 5")
        bounded_limit = min(50, max(1, limit))
        if bounded_limit != limit and adjustments is not None:
            adjustments.append(f"result limit {limit} clamped to {bounded_limit}")
        query["limit"] = bounded_limit
        scope = _slug(query.get("scope", "all"))
        scope_aliases = {
            "vulnerable_only": "vulnerable", "priority": "vulnerable",
            "priority_tracts": "vulnerable", "population": "all", "everyone": "all",
        }
        query["scope"] = scope_aliases.get(scope, scope)
        if query["scope"] not in {"all", "vulnerable"}:
            query["scope"] = "all"
            if adjustments is not None:
                adjustments.append(f"invalid result scope '{scope}' replaced with all")
        query["raw_text"] = str(query.get("raw_text") or query.get("question") or user_prompt).strip()
        query.pop("question", None)
        normalized.append(query)
    return normalized[:8]


def _append_note(raw: dict[str, Any], note: str) -> None:
    """Append one concise, user-visible parser adjustment without duplicating it."""
    current = str(raw.get("notes") or "").strip()
    if note not in current:
        raw["notes"] = f"{current} [{note}]".strip()


def _bounded_number(
    value: Any, cast, minimum: float, maximum: float | None = None, *, strict_minimum: bool = False,
) -> int | float | None:
    """Return one finite bounded number, or None when it cannot satisfy the field contract."""
    try:
        parsed = float(value)
        if isinstance(value, bool) or not math.isfinite(parsed) or (cast is int and not parsed.is_integer()):
            return None
        number = cast(parsed)
    except (TypeError, ValueError, OverflowError):
        return None
    below = number <= minimum if strict_minimum else number < minimum
    if below or (maximum is not None and number > maximum):
        return None
    return number


def _single_explicit_number(user_prompt: str, patterns: tuple[str, ...]) -> int | float | None:
    """Extract one unnegated scalar only when all matching phrases agree on its value."""
    values: list[float] = []
    for pattern in patterns:
        for match in re.finditer(pattern, user_prompt, re.I):
            before = user_prompt[max(0, match.start() - 35):match.start()]
            if re.search(r"\b(?:not|no|without|exclude|excluding|avoid|avoiding)\b[^.!?;]*$", before, re.I):
                continue
            values.append(float(match.group(1)))
    unique = list(dict.fromkeys(values))
    return unique[0] if len(unique) == 1 else None


def _mentioned_profile_names(user_prompt: str, profiles: dict[str, Any]) -> list[str]:
    """Find deliberate profile references while abstaining on negation and generic one-word labels."""
    text = re.sub(r"[-_]+", " ", user_prompt.lower())
    matches: list[str] = []
    for name, profile in profiles.items():
        name_phrase = re.sub(r"[-_]+", " ", name.lower()).strip()
        label_phrase = re.sub(r"[-_]+", " ", str(profile.get("label", "")).lower()).strip()
        phrases = [name_phrase]
        if len(label_phrase.split()) >= 2:
            phrases.append(label_phrase)
        for phrase in dict.fromkeys(item for item in phrases if item):
            pattern = r"(?<!\w)" + r"\s+".join(map(re.escape, phrase.split())) + r"(?!\w)"
            for match in re.finditer(pattern, text, re.I):
                before = text[max(0, match.start() - 45):match.start()]
                after = text[match.end():min(len(text), match.end() + 25)]
                if re.search(r"\b(?:not|do not|don't|without|exclude|excluding|avoid)\b[^.!?;]*$", before, re.I):
                    continue
                deliberate = len(phrase.split()) >= 2 or re.search(
                    r"\b(?:use|apply|choose|select|profile|weights?|setting)\b[^.!?;]*$", before, re.I,
                ) or re.search(r"^\s*(?:profile|weights?|setting)\b", after, re.I)
                if deliberate:
                    matches.append(name)
                    break
            if name in matches:
                break
    return list(dict.fromkeys(matches))


def _normalize_ambiguities(value: Any) -> list[dict[str, Any]]:
    """Keep only typed, answerable ambiguities; optional malformed entries cannot fail parsing."""
    allowed = {
        "place_names", "n_facilities", "threshold_minutes", "analysis_goal",
        "mobility_scenario", "result_queries",
    }
    aliases = {"place_name": "place_names", "places": "place_names", "result_query": "result_queries"}
    items = value if isinstance(value, list) else []
    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        field = aliases.get(_slug(item.get("field")), _slug(item.get("field")))
        question = str(item.get("question") or "").strip()
        if field not in allowed or not question:
            continue
        candidates = item.get("candidates") if isinstance(item.get("candidates"), list) else []
        result.append({
            "field": field,
            "candidates": [str(candidate).strip() for candidate in candidates if str(candidate).strip()][:10],
            "question": question,
        })
    return result[:8]


def _converge_task_payload(raw: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Converge unsafe provider variants before strict TaskSpec validation."""
    enum_rules = {
        "intervention": (
            {"cooling_center", "mobile_clinic", "monitoring_station"},
            {"cooling_centre": "cooling_center", "cooling_facility": "cooling_center"},
            "cooling_center",
        ),
        "analysis_goal": (
            {"facility_siting", "tract_review", "heat_review", "minimum_facilities", "minimum_threshold",
             "reachability_review", "scenario_comparison", "sensitivity_analysis"},
            {"site_selection": "facility_siting", "siting": "facility_siting", "optimization": "facility_siting",
             "equity_analysis": "tract_review", "vulnerability_analysis": "tract_review",
             "diagnostic": "tract_review", "comparison": "scenario_comparison"},
            "facility_siting",
        ),
        "candidate_source": ({"osm", "csv"}, {"openstreetmap": "osm", "upload": "csv"}, "osm"),
        "mobility_scenario": (
            {"walking", "cycling", "driving", "walk_drive_equity"},
            {"walk": "walking", "on_foot": "walking", "bike": "cycling", "biking": "cycling",
             "drive": "driving", "car": "driving"},
            "walking",
        ),
        "coverage_group": (
            {"all", "vulnerable"},
            {"vulnerable_only": "vulnerable", "priority": "vulnerable", "everyone": "all"},
            "all",
        ),
    }
    for field, (allowed, aliases, default) in enum_rules.items():
        original = raw.get(field)
        if original in (None, ""):
            raw[field] = default
            continue
        value = aliases.get(_slug(original), _slug(original))
        if value not in allowed:
            raw[field] = default
            _append_note(raw, f"invalid {field} '{original}' replaced with {default}")
        else:
            raw[field] = value

    numeric_rules = {
        "n_facilities": (int, 1, 100, defaults["n_facilities"], False),
        "threshold_minutes": (float, 0.0, None, defaults["threshold_minutes"], True),
        "coverage_target": (float, 0.0, 1.0, None, False),
        "equity_alpha": (float, 0.0, 1.0, None, False),
        "facility_capacity": (float, 0.0, None, None, True),
    }
    for field, (cast, minimum, maximum, default, strict_minimum) in numeric_rules.items():
        value = raw.get(field)
        if value in (None, ""):
            raw[field] = default
            continue
        number = _bounded_number(
            value, cast, minimum, maximum, strict_minimum=strict_minimum,
        )
        if number is None:
            raw[field] = default
            _append_note(raw, f"invalid {field} '{value}' replaced with {default}")
            continue
        raw[field] = number

    mobility_aliases = enum_rules["mobility_scenario"][1]
    recommended_mode = raw.get("recommended_mobility_scenario")
    if recommended_mode in (None, ""):
        raw["recommended_mobility_scenario"] = None
    else:
        normalized_mode = mobility_aliases.get(_slug(recommended_mode), _slug(recommended_mode))
        if normalized_mode in enum_rules["mobility_scenario"][0]:
            raw["recommended_mobility_scenario"] = normalized_mode
        else:
            raw["recommended_mobility_scenario"] = None
            _append_note(raw, f"invalid recommended mobility '{recommended_mode}' omitted")

    for field in ("place_name", "candidate_file", "recommended_value_profile", "operating_window"):
        if field == "operating_window":
            continue
        value = raw.get(field)
        if value is not None and not isinstance(value, str):
            raw[field] = str(value) if field == "place_name" else None
            _append_note(raw, f"invalid {field} value normalized")
    amenities = raw.get("candidate_amenities")
    if not isinstance(amenities, list):
        raw["candidate_amenities"] = [
            "library", "community_centre", "school", "sports_centre",
            "arts_centre", "place_of_worship",
        ]
        if amenities not in (None, ""):
            _append_note(raw, "invalid candidate amenities replaced with supported defaults")
    else:
        raw["candidate_amenities"] = list(dict.fromkeys(
            _slug(item) for item in amenities if _slug(item)
        ))

    def bounded_numbers(field: str, cast, minimum: float, maximum: float | None, maximum_items: int) -> None:
        values = raw.get(field)
        if values in (None, ""):
            raw[field] = []
            return
        if not isinstance(values, list):
            values = [values]
        normalized = []
        for value in values:
            try:
                parsed = float(value)
                if not math.isfinite(parsed) or (cast is int and not parsed.is_integer()):
                    raise ValueError
                number = cast(parsed)
                if number < minimum or (maximum is not None and number > maximum):
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                _append_note(raw, f"invalid value omitted from {field}")
                continue
            if number not in normalized:
                normalized.append(number)
        raw[field] = normalized[:maximum_items]

    bounded_numbers("sensitivity_thresholds", float, 1e-9, None, 12)
    bounded_numbers("recommended_sensitivity_thresholds", float, 1e-9, None, 12)
    bounded_numbers("sensitivity_facility_counts", int, 1, 100, 12)
    bounded_numbers("recommended_sensitivity_facility_counts", int, 1, 100, 12)
    profiles = raw.get("comparison_profiles")
    raw["comparison_profiles"] = (
        list(dict.fromkeys(str(value).strip() for value in profiles if str(value).strip()))[:6]
        if isinstance(profiles, list) else []
    )

    for field, model in (("route_origin", RouteOriginSpec), ("operating_window", OperatingWindow)):
        value = raw.get(field)
        if value in (None, ""):
            raw[field] = None
            continue
        try:
            raw[field] = model.model_validate(value).model_dump(mode="json")
        except Exception:
            raw[field] = None
            _append_note(raw, f"invalid {field} omitted")
    requirements = raw.get("regional_requirements")
    valid_requirements = []
    for item in requirements if isinstance(requirements, list) else []:
        try:
            valid_requirements.append(RegionalRequirement.model_validate(item).model_dump())
        except Exception:
            _append_note(raw, "invalid regional requirement omitted")
    raw["regional_requirements"] = valid_requirements
    if raw.get("weights") is not None and not isinstance(raw.get("weights"), dict):
        raw["weights"] = None
        _append_note(raw, "invalid weights omitted")

    raw["ambiguities"] = _normalize_ambiguities(raw.get("ambiguities"))
    if raw.get("coverage_target") is not None and raw.get("equity_alpha") is not None:
        raw["ambiguities"].append({
            "field": "analysis_goal",
            "candidates": ["hard coverage target", "efficiency-equality trade-off"],
            "question": "Should this run enforce the coverage target or select from the efficiency-Gini trade-off?",
        })
        raw["equity_alpha"] = None
        _append_note(raw, "equity alpha held for confirmation because a hard coverage target is active")
    return raw


def _matching_profile(weights: Any, profiles: dict[str, Any], tolerance: float = 0.005) -> str | None:
    """Match complete structured weights numerically instead of comparing JSON text."""
    if not isinstance(weights, dict):
        return None
    try:
        numeric = {key: float(value) for key, value in weights.items()}
        if numeric and all(0.0 <= value <= 100.0 for value in numeric.values()) and math.isclose(
            sum(numeric.values()), 100.0, abs_tol=0.5, rel_tol=0.0,
        ):
            numeric = {key: value / 100.0 for key, value in numeric.items()}
        candidate = ValueWeights(**numeric).model_dump()
    except Exception:
        return None
    matches = []
    for name, profile in profiles.items():
        try:
            expected = ValueWeights(**profile.get("weights", {})).model_dump()
        except Exception:
            continue
        if all(math.isclose(candidate[key], expected[key], abs_tol=tolerance, rel_tol=0.0) for key in expected):
            matches.append(name)
    return matches[0] if len(matches) == 1 else None


def _normalize_place_names(value: Any, place_name: Any) -> list[str]:
    """Normalize the LLM's explicit place list without geocoding inside the parser."""
    values = value if isinstance(value, list) else [value] if isinstance(value, str) else []
    if not values and isinstance(place_name, str) and place_name.strip():
        values = [place_name]
    result: list[str] = []
    for item in values:
        name = str(item).strip()
        if name and name.casefold() not in {existing.casefold() for existing in result}:
            result.append(name)
    return result[:5]


class LLMClient:
    """OpenAI-compatible Chat Completions client."""

    def __init__(self, cfg: dict, api_key: str | None = None):
        """Configure an OpenAI-compatible client without persisting credentials."""
        from openai import OpenAI
        self.cfg = cfg
        provider = str(cfg.get("provider", "openrouter")).lower()
        if provider not in {"gemini", "openrouter", "openai", "custom", "local"}:
            raise ValueError(f"Unsupported LLM provider: {provider}")
        env_name = {
            "gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
        }.get(provider, "OPENAI_API_KEY")
        api_key = api_key or cfg.get("_session_api_key") or os.getenv(env_name)
        if provider == "local" and not api_key:
            api_key = "local-model"
        if not api_key:
            raise RuntimeError(f"{env_name} is missing. Configure a session key or the project environment.")
        headers = {}
        if provider == "openrouter" and os.getenv("OPENROUTER_SITE_URL"): headers["HTTP-Referer"] = os.getenv("OPENROUTER_SITE_URL")
        if provider == "openrouter" and os.getenv("OPENROUTER_APP_NAME"): headers["X-OpenRouter-Title"] = os.getenv("OPENROUTER_APP_NAME")
        self.client = OpenAI(base_url=cfg.get("base_url", "https://openrouter.ai/api/v1"), api_key=api_key,
                             default_headers=headers or None)

    def _completion(self, system: str, user: str, *, mode: str, schema: dict | None = None):
        """Return one provider response through a bounded structured-output strategy."""
        if mode == "prompt":
            system = (
                system.rstrip()
                + "\n\nReturn exactly one valid JSON object. Start with { and end with }. "
                  "Do not include markdown, analysis, comments, or explanatory text."
            )
            if schema:
                system += "\nThe JSON must satisfy this schema:\n" + json.dumps(schema)
        kwargs = dict(
            model=self.cfg["model"], messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=self.cfg.get("max_tokens", 4096), temperature=self.cfg.get("temperature", 0.0))
        if mode == "schema" and schema:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "oasis_structured_response", "schema": schema},
            }
        elif mode == "json":
            kwargs["response_format"] = {"type": "json_object"}
        elif mode == "tool":
            kwargs["tools"] = [{
                "type": "function",
                "function": {
                    "name": "submit_json",
                    "description": "Submit the requested structured result.",
                    "parameters": schema or {
                        "type": "object",
                        "properties": {
                            "payload": {
                                "type": "string",
                                "description": "The complete requested JSON object encoded as a string.",
                            }
                        },
                        "required": ["payload"],
                        "additionalProperties": False,
                    },
                },
            }]
            kwargs["tool_choice"] = {
                "type": "function", "function": {"name": "submit_json"}
            }
            kwargs["parallel_tool_calls"] = False
        if self.cfg.get("provider") == "openrouter":
            effort = (
                self.cfg.get("task_parsing_reasoning_effort", "medium")
                if schema and schema.get("title") == "TaskSpec"
                else self.cfg.get("reasoning_effort", "low")
            )
            kwargs["extra_body"] = {
                "provider": {"require_parameters": mode == "schema"},
                "reasoning": {"effort": effort, "exclude": True},
            }
        elif self.cfg.get("provider") == "gemini":
            kwargs["reasoning_effort"] = (
                self.cfg.get("task_parsing_reasoning_effort", "medium")
                if schema and schema.get("title") == "TaskSpec"
                else self.cfg.get("reasoning_effort", "low")
            )
        return self.client.chat.completions.create(**kwargs)

    @staticmethod
    def _json_payload(response) -> dict:
        """Parse the first JSON message from one provider response."""
        if not getattr(response, "choices", None):
            raise ValueError("The selected model returned no completion choices.")
        choice = response.choices[0]
        message = choice.message
        parsed = getattr(message, "parsed", None)
        if isinstance(parsed, dict):
            return parsed
        tool_calls = getattr(message, "tool_calls", None) or []
        for call in tool_calls:
            arguments = getattr(getattr(call, "function", None), "arguments", None)
            if isinstance(arguments, str):
                try:
                    payload = json.loads(arguments)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and set(payload) == {"payload"}:
                    nested = payload["payload"]
                    if isinstance(nested, str):
                        try:
                            nested = json.loads(nested)
                        except json.JSONDecodeError:
                            continue
                    payload = nested
                if isinstance(payload, dict):
                    return payload
        content = getattr(message, "content", None)
        if isinstance(content, list):
            text = "\n".join(
                str(item.get("text", "") if isinstance(item, dict) else getattr(item, "text", ""))
                for item in content
            )
        else:
            text = content
        if not text:
            finish_reason = getattr(choice, "finish_reason", None)
            if finish_reason in {"length", "max_tokens"}:
                raise ValueError(
                    "The selected model exhausted its output-token budget before returning JSON."
                )
            raise ValueError("The selected model returned empty content instead of the required JSON.")
        cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", str(text).strip(), flags=re.I)
        decoder = json.JSONDecoder()
        for index, character in enumerate(cleaned):
            if character not in "{[":
                continue
            try:
                payload, _ = decoder.raw_decode(cleaned[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
        raise ValueError(
            "The selected model returned text instead of the required JSON object. "
            "Try the request again or choose a model that supports structured JSON output."
        )

    @staticmethod
    def _non_retryable_provider_error(exc: Exception) -> str | None:
        """Translate provider failures that no structured-output retry can repair."""
        detail = str(exc)
        lowered = detail.lower()
        if "user location is not supported for the api use" in lowered:
            return (
                "Gemini rejected the request because the OASIS backend's network location is not "
                "supported by the Gemini Developer API. This is a provider-region restriction, not "
                "a JSON-format error. In Preliminary setup, choose OpenRouter, OpenAI, or a local "
                "model, or run the backend in a Gemini-supported region."
            )
        return None

    def _structured_response(
        self, system: str, user: str, schema: dict | None = None,
    ) -> tuple[dict, Any]:
        """Try JSON mode, forced function calling, then a JSON-only prompt."""
        failures = []
        modes = ("schema", "json", "tool", "prompt") if schema else ("json", "tool", "prompt")
        for mode in modes:
            try:
                response = self._completion(system, user, mode=mode, schema=schema)
                return self._json_payload(response), response
            except Exception as exc:
                terminal = self._non_retryable_provider_error(exc)
                if terminal:
                    raise RuntimeError(terminal) from exc
                failures.append(exc)
        detail = str(failures[-1]) if failures else "unknown structured-output failure"
        raise ValueError(
            f"The selected model could not produce a JSON object after JSON mode, "
            f"function-call, and prompt compatibility attempts: {detail}"
        ) from failures[-1]

    def structured(self, system: str, user: str, schema: dict | None = None) -> dict:
        """Call the LLM and parse JSON across OpenAI-compatible provider variants."""
        return self._structured_response(system, user, schema=schema)[0]

    def probe(self, system: str, user: str) -> tuple[dict, str]:
        """Return a parsed probe plus the model identifier reported by the provider."""
        payload, response = self._structured_response(system, user)
        return payload, str(getattr(response, "model", self.cfg["model"]))

    def list_models(self) -> list[str]:
        """Return model IDs exposed by the configured OpenAI-compatible endpoint."""
        response = self.client.models.list()
        return sorted({str(item.id) for item in response.data if getattr(item, "id", None)})


class Orchestrator:
    """Coordinate typed LLM decisions with deterministic registered tools."""

    def __init__(self, llm: LLMClient, cfg: dict):
        """Create an orchestration session backed by the supplied LLM client."""
        self.llm, self.cfg = llm, cfg
        self.ctx = Context(); self.ctx.put("config", cfg)

    def parse_task(self, user_prompt: str) -> TaskSpec:
        """Parse a request and deterministically fill vague numeric defaults."""
        defaults = {"n_facilities": self.cfg.get("optimization", {}).get("n_facilities", 10),
                    "threshold_minutes": self.cfg.get("accessibility", {}).get("threshold_minutes", 15)}
        runtime = {"defaults": defaults, "value_profiles": self.cfg.get("value_profiles", {})}
        raw = self.llm.structured(
            load_prompt("task_parsing"),
            f"Runtime configuration: {json.dumps(runtime)}\nUser request: {user_prompt}",
            schema=TaskSpec.model_json_schema(),
        )
        if not isinstance(raw, dict):
            raw = {}
        route_origin_field_supplied = "route_origin" in raw
        notes = raw.get("notes")
        if isinstance(notes, list):
            raw["notes"] = "; ".join(str(item) for item in notes if str(item).strip())
        elif isinstance(notes, dict):
            raw["notes"] = json.dumps(notes, sort_keys=True)
        elif not isinstance(notes, str):
            raw["notes"] = "" if notes is None else str(notes)
        llm_count = raw.get("n_facilities")
        llm_threshold = raw.get("threshold_minutes")
        legacy_query = raw.pop("tract_ranking", None)
        query_adjustments: list[str] = []
        raw["result_queries"] = _normalize_result_queries(
            raw.get("result_queries") or legacy_query, user_prompt, query_adjustments
        )
        for adjustment in query_adjustments:
            _append_note(raw, adjustment)
        legacy_places = raw.pop("study_areas", None)
        raw["place_names"] = _normalize_place_names(
            raw.get("place_names") or legacy_places, raw.get("place_name")
        )
        if raw["place_names"]:
            raw["place_name"] = raw["place_names"][0]
        raw = _converge_task_payload(raw, defaults)
        ambiguities = list(raw["ambiguities"])
        if len(raw["place_names"]) > 1 and not any(
            isinstance(item, dict) and item.get("field") == "place_names" for item in ambiguities
        ):
            ambiguities.append({
                "field": "place_names", "candidates": raw["place_names"],
                "question": "Which study area should this run use?",
            })
        if raw.get("analysis_goal") == "tract_review" and not raw["result_queries"]:
            ambiguities.append({
                "field": "result_queries", "candidates": [
                    "heat exposure", "vulnerability", "access disadvantage",
                    "combined disadvantage", "coverage gap",
                ],
                "question": "Which available tract metric should be ranked?",
            })
        raw["ambiguities"] = ambiguities
        has_structured_counts = bool(
            raw.get("regional_requirements") or raw.get("sensitivity_facility_counts")
        )
        explicit_count = None if has_structured_counts else _single_explicit_number(user_prompt, (
            r"\b(?:select|choose|site|locate|open|recommend)\s+(?:up\s+to\s+)?(-?\d+)\b",
            r"(?<![\w.])(-?\d+)\s+(?:candidate\s+)?(?:cooling[- ]?)?"
            r"(?:centers?|centres?|facilities|sites?)\b",
        ))
        if explicit_count is not None:
            bounded_count = _bounded_number(explicit_count, int, 1, 100)
            parsed_count = raw.get("n_facilities")
            valid_llm_count = _bounded_number(llm_count, int, 1, 100)
            if bounded_count is None:
                fallback_count = int(parsed_count)
                _append_note(raw, (
                    f"requested facility count {explicit_count:g} is outside the supported range "
                    f"1-100; retained {fallback_count}"
                ))
                ambiguities.append({
                    "field": "n_facilities", "candidates": [str(fallback_count)],
                    "question": (
                        f"OASIS supports 1 to 100 facilities per run. Continue with {fallback_count}, "
                        "or divide the study into separate runs?"
                    ),
                })
            else:
                if valid_llm_count is not None and valid_llm_count != bounded_count:
                    ambiguities.append({
                        "field": "n_facilities",
                        "candidates": [str(valid_llm_count), str(bounded_count)],
                        "question": (
                            f"Should this run select {valid_llm_count} or {bounded_count} facilities?"
                        ),
                    })
                raw["n_facilities"] = bounded_count
            if bounded_count is not None and raw.get("analysis_goal") == "minimum_facilities":
                ambiguities.append({
                    "field": "analysis_goal",
                    "candidates": ["facility_siting", "minimum_facilities"],
                    "question": (
                        "Is the stated facility count a fixed siting budget, or should OASIS calculate "
                        "the minimum number required?"
                    ),
                })
        explicit_threshold = None if raw.get("sensitivity_thresholds") else _single_explicit_number(
            user_prompt,
            (r"(?<![\w.])(-?\d+(?:\.\d+)?)\s*(?:-\s*)?(?:minutes?|mins?)\b",),
        )
        if explicit_threshold is not None:
            bounded_threshold = _bounded_number(
                explicit_threshold, float, 0, strict_minimum=True,
            )
            parsed_threshold = raw.get("threshold_minutes")
            valid_llm_threshold = _bounded_number(
                llm_threshold, float, 0, strict_minimum=True,
            )
            if bounded_threshold is None:
                fallback_threshold = float(parsed_threshold)
                _append_note(raw, (
                    f"requested threshold {explicit_threshold:g} minutes is not positive; "
                    f"retained {fallback_threshold:g} minutes"
                ))
                ambiguities.append({
                    "field": "threshold_minutes", "candidates": [f"{fallback_threshold:g}"],
                    "question": (
                        f"Travel time must be positive. Continue with "
                        f"{fallback_threshold:g} minutes?"
                    ),
                })
            else:
                if (valid_llm_threshold is not None and not math.isclose(
                    valid_llm_threshold, bounded_threshold, abs_tol=1e-9
                )):
                    ambiguities.append({
                        "field": "threshold_minutes",
                        "candidates": [
                            f"{valid_llm_threshold:g}", f"{bounded_threshold:g}",
                        ],
                        "question": (
                            f"Should this run use a {valid_llm_threshold:g}-minute or "
                            f"{bounded_threshold:g}-minute threshold?"
                        ),
                    })
                raw["threshold_minutes"] = bounded_threshold
        route_origin = raw.get("route_origin")
        fallback_origin = route_origin_from_prompt(user_prompt)
        model_entity = str(route_origin.get("entity") or "").strip() if isinstance(route_origin, dict) else ""
        destination_as_origin = bool(re.fullmatch(
            r"(?:the\s+)?(?:nearest|closest)?\s*(?:cooling\s+)?"
            r"(?:cent(?:er|re)|facility|facilities|site|sites)",
            model_entity,
            re.I,
        ))
        if fallback_origin and (
            not route_origin_field_supplied or not model_entity or destination_as_origin
        ):
            route_origin = ({
                "entity": fallback_origin, "context": "", "positional_hint": "",
                "raw_text": fallback_origin, "source": "fallback",
            })
        if route_origin:
            raw["route_origin"] = route_origin
            raw["analysis_goal"] = "facility_siting"
            # A point-route request prepares candidates and then routes from the
            # confirmed origin. It must not inherit a model-invented hard
            # study-area coverage constraint, which would turn routing into LSCP.
            raw["coverage_target"] = None
            if explicit_count is None:
                raw["n_facilities"] = defaults["n_facilities"]
        for field in ("notes", "recommendation_rationale", "preference_question"):
            value = raw.get(field)
            if isinstance(value, list):
                raw[field] = "; ".join(str(item) for item in value if str(item).strip())
            elif isinstance(value, dict):
                raw[field] = json.dumps(value, sort_keys=True)
            elif not isinstance(value, str):
                raw[field] = "" if value is None else str(value)
        equity_mode = re.search(
            r"\b(?:walk[-_ ]?drive[-_ ]?equity|car[- ]?(?:access|availability)[-_ ]?equity)\b",
            user_prompt, re.I,
        )
        mode_patterns = {
            "walking": r"\b(?:walk(?:ing)?|on\s+foot)\b|\u6b65\u884c",
            "cycling": r"\b(?:cycl(?:e|ing)|bik(?:e|ing)|bicycle)\b|\u9a91\u884c",
            "driving": (
                r"\b(?:driv(?:e|ing)|by\s+(?:a\s+)?cars?|via\s+(?:a\s+)?cars?|"
                r"car[- ]?(?:travel|routing|accessibility)|automobiles?)\b|\u9a7e\u8f66|\u5f00\u8f66"
            ),
        }
        detected_modes = [
            mode for mode, pattern in mode_patterns.items()
            if re.search(pattern, user_prompt, re.I)
        ]
        explicit_mode = "walk_drive_equity" if equity_mode else detected_modes[0] if len(detected_modes) == 1 else None
        if explicit_mode:
            ambiguities = [
                item for item in ambiguities
                if not (isinstance(item, dict) and item.get("field") == "mobility_scenario")
            ]
            if raw.get("mobility_scenario") != explicit_mode:
                raw["mobility_scenario"] = explicit_mode
                _append_note(raw, f"explicit travel mode '{explicit_mode}' retained from the request")
        profiles, weights = runtime["value_profiles"], raw.get("weights")
        normalized_prompt = re.sub(r"[-_]+", " ", user_prompt.lower())
        mentioned_profiles = _mentioned_profile_names(user_prompt, profiles)
        compares_efficiency_equity = bool(
            re.search(r"\bcompare\b", normalized_prompt)
            and re.search(r"\b(efficien(?:cy|t)|coverage|access)\b", normalized_prompt)
            and re.search(r"\b(equit(?:y|able)|inequal(?:ity)?|gini|fairness)\b", normalized_prompt)
        )
        if compares_efficiency_equity:
            raw["analysis_goal"] = "scenario_comparison"
            raw["result_queries"] = [
                query for query in raw.get("result_queries", [])
                if not (
                    isinstance(query, dict)
                    and re.search(r"\bcompare\b", str(query.get("raw_text") or ""), re.I)
                    and re.search(
                        r"\b(?:inequal(?:ity)?|equity|gini|fairness)\b",
                        str(query.get("raw_text") or ""),
                        re.I,
                    )
                )
            ]
        if raw.get("analysis_goal") == "scenario_comparison":
            selected_profiles = list(dict.fromkeys(mentioned_profiles))
            if len(selected_profiles) < 2 and compares_efficiency_equity:
                selected_profiles = [
                    name for name in ("efficiency_first", "equity_first") if name in profiles
                ]
            raw["comparison_profiles"] = selected_profiles
        explicit_profile_names = list(dict.fromkeys(mentioned_profiles))
        matched_profile = _matching_profile(weights, profiles)
        if len(explicit_profile_names) == 1:
            selected_profile = explicit_profile_names[0]
            raw["recommended_value_profile"] = selected_profile
            raw["weights"] = None
        elif weights is not None and matched_profile is None:
            raw["weights"] = None
            _append_note(raw, "unregistered weights require HITL (human confirmation)")
        elif weights is not None:
            raw["recommended_value_profile"] = raw.get("recommended_value_profile") or matched_profile
            raw["weights"] = None
        if raw.get("recommended_value_profile") not in profiles:
            raw["recommended_value_profile"] = None
        raw["ambiguities"] = _normalize_ambiguities(ambiguities)
        raw = _converge_task_payload(raw, defaults)
        place = raw.get("place_name")
        self.ctx.record("parse", "Natural-language request parsed into a proposed task.",
                        fields={k: raw.get(k) for k in ("place_name", "analysis_goal", "candidate_source", "n_facilities",
                                                        "threshold_minutes", "coverage_target", "coverage_group", "mobility_scenario",
                                                        "operating_window", "recommended_value_profile",
                                                        "recommended_mobility_scenario", "recommendation_rationale",
                                                        "sensitivity_thresholds", "sensitivity_facility_counts",
                                                        "recommended_sensitivity_thresholds",
                                                        "recommended_sensitivity_facility_counts",
                                                        "regional_requirements", "place_names", "result_queries", "ambiguities",
                                                        "preference_question", "notes")})
        if not isinstance(place, str) or not place.strip():
            partial = TaskSpec(**{**raw, "place_name": ""}).model_dump(mode="json")
            raise MissingPlaceError(partial)
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
                        coverage_target=task.coverage_target, coverage_group=task.coverage_group,
                        facility_capacity=task.facility_capacity, equity_alpha=task.equity_alpha)
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
        elif step.tool == "run_sensitivity_analysis":
            args.update(
                threshold_minutes=task.sensitivity_thresholds or [task.threshold_minutes],
                facility_counts=task.sensitivity_facility_counts or [task.n_facilities],
                weights=task.weights.model_dump() if task.weights else None,
                coverage_group=task.coverage_group,
                facility_capacity=task.facility_capacity,
            )
        elif step.tool == "compare_value_profiles":
            args.update(profiles=task.comparison_profiles,
                        n_facilities=task.n_facilities,
                        threshold_minutes=task.threshold_minutes,
                        facility_capacity=task.facility_capacity)
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
        registered_names = {item["name"] for item in registry.describe()}
        if payload.get("kind") in registered_names and not payload.get("tool"):
            payload["tool"] = payload["kind"]
            payload["kind"] = "tool"
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
        """Return the handle proving the primary executable goal is complete."""
        return {"facility_siting": "outputs", "tract_review": "result_query_answers", "heat_review": "heat_summary",
                "minimum_facilities": "minimum_facilities", "minimum_threshold": "minimum_threshold",
                "reachability_review": "reachability_analysis",
                "scenario_comparison": "scenario_comparison",
                "sensitivity_analysis": "sensitivity_results"}[task.analysis_goal]

    def _is_complete(self, task: TaskSpec) -> bool:
        """Require the primary handle and every confirmed read-only result query."""
        if not self.ctx.has(self._goal_handle(task)):
            return False
        if (
            task.analysis_goal == "minimum_facilities"
            and not self.ctx.get("minimum_facilities", {}).get("feasible")
            and not self.ctx.has("minimum_threshold")
        ):
            return False
        if not task.result_queries:
            return True
        answers = self.ctx.get("result_query_answers", {}).get("answers", [])
        return len(answers) == len(task.result_queries)

    def _validated_changes(self, task: TaskSpec, changes: dict[str, Any]) -> dict[str, Any]:
        """Keep only typed human-editable changes proposed by the model."""
        allowed = {"n_facilities", "threshold_minutes", "coverage_target", "candidate_amenities"}
        filtered = {key: value for key, value in changes.items() if key in allowed}
        amenities = filtered.get("candidate_amenities")
        if amenities and "school" not in task.candidate_amenities:
            filtered["candidate_amenities"] = [item for item in amenities if item != "school"]
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
            self.ctx.check_cancelled()
            self.ctx.record("tool_call", f"Calling {step.tool}.", step.tool, args=step.args, attempt=attempt + 1)
            try: result: ToolResult = registry.get(step.tool).fn(ctx=self.ctx, **step.args)
            except RunCancelled:
                raise
            except Exception as exc:
                result = ToolResult(ok=False, tool=step.tool, error=ToolError(kind="unknown", message=str(exc)))
            issue = self._quality_issue(step, result) if result.ok else None
            self.ctx.check_cancelled()
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
            except RunCancelled:
                raise
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
        primary_ready = self.ctx.has(self._goal_handle(task))
        if (
            task.result_queries
            and not self.ctx.has("result_query_answers")
            and (task.analysis_goal == "tract_review" or primary_ready)
        ):
            if not self.ctx.has("study"): return "resolve_study_area"
            if not self.ctx.has("demand"): return "prepare_demand"
            metrics = {query.metric for query in task.result_queries}
            if metrics & {"access_disadvantage", "combined_disadvantage", "coverage_gap"}:
                if not self.ctx.has("candidates"): return (
                    "load_candidate_sites" if task.candidate_source == "csv" else "fetch_candidate_sites"
                )
                if not self.ctx.has("travel"): return "build_travel_matrix"
            return "answer_result_queries"
        if task.analysis_goal == "tract_review":
            return None
        if task.analysis_goal == "heat_review":
            if not self.ctx.has("study"): return "resolve_study_area"
            if not self.ctx.has("demand"): return "prepare_demand"
            return "summarize_heat_exposure"
        if task.analysis_goal in {"minimum_facilities", "minimum_threshold", "reachability_review"}:
            if not self.ctx.has("study"): return "resolve_study_area"
            if not self.ctx.has("demand"): return "prepare_demand"
            if not self.ctx.has("candidates"): return (
                "load_candidate_sites" if task.candidate_source == "csv" else "fetch_candidate_sites"
            )
            if not self.ctx.has("travel"): return "build_travel_matrix"
            if task.analysis_goal == "minimum_facilities":
                if not self.ctx.has("minimum_facilities"):
                    return "find_minimum_facilities"
                if (
                    not self.ctx.get("minimum_facilities", {}).get("feasible")
                    and not self.ctx.has("minimum_threshold")
                ):
                    return "find_minimum_threshold"
                return None
            return {
                "minimum_threshold": "find_minimum_threshold",
                "reachability_review": "analyze_reachability",
            }[task.analysis_goal]
        if task.analysis_goal in {"sensitivity_analysis", "scenario_comparison"}:
            if not self.ctx.has("study"): return "resolve_study_area"
            if not self.ctx.has("demand"): return "prepare_demand"
            if not self.ctx.has("candidates"): return (
                "load_candidate_sites" if task.candidate_source == "csv" else "fetch_candidate_sites"
            )
            if not self.ctx.has("travel"): return "build_travel_matrix"
        if task.analysis_goal == "sensitivity_analysis":
            return "run_sensitivity_analysis"
        if task.analysis_goal == "scenario_comparison":
            if not self.ctx.has("built_environment"): return "prepare_built_environment"
            return "compare_value_profiles"
        if task.analysis_goal != "facility_siting": return None
        if task.coverage_target is not None and self.ctx.has("travel") and not self.ctx.has("analysis"):
            return "optimize_sites"
        if not self.ctx.has("analysis"): return None
        solution = self.ctx.get("analysis", {}).get("solution")
        if getattr(solution, "solver_status", None) not in {"Optimal", "Feasible"}: return None
        if self.ctx.has("demand") and not self.ctx.has("built_environment"):
            return "prepare_built_environment"
        if not self.ctx.has("outputs"): return "generate_outputs"
        return None

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
        errors = [item.error for item in results if item.error]
        error = next(
            (
                item for item in reversed(errors)
                if not (
                    item.kind == "constraint_conflict"
                    and item.message.startswith("The same failed tool call")
                )
            ),
            errors[-1] if errors else None,
        )
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
        solution = analysis["solution"]
        quality = self.ctx.get("quality", {})
        vulnerability = self.ctx.get("vulnerability_analysis", {})
        routing = self.ctx.get("travel", {}).get("routing_report", {})
        query_answers = self.ctx.get("result_query_answers", {}).get("answers", [])
        evidence = {
            "task": {
                "place_name": task.place_name, "analysis_goal": task.analysis_goal,
                "n_facilities": task.n_facilities, "threshold_minutes": task.threshold_minutes,
                "mobility_scenario": task.mobility_scenario, "coverage_target": task.coverage_target,
                "equity_alpha": task.equity_alpha,
            },
            "solution": {
                "profile_name": solution.profile_name, "selected_ids": solution.selected_ids,
                "covered_ratio": solution.covered_ratio,
                "vulnerable_covered_ratio": solution.vulnerable_covered_ratio,
                "mean_access_score": solution.mean_access_score,
                "population_weighted_access_gini": solution.population_weighted_access_gini,
                "mean_travel_minutes": solution.mean_travel_minutes,
                "solver_status": solution.solver_status,
            },
            "frontier": [{"alpha": p.alpha, "efficiency_metric": p.efficiency_metric,
                          "equity_metric": p.equity_metric,
                          "population_weighted_gini": p.population_weighted_gini}
                         for p in analysis["frontier"]],
            "quality": {
                "operating_hours": quality.get("operating_hours"),
                "public_access": quality.get("public_access"),
                "heat": quality.get("heat"),
            },
            "vulnerability_diagnostics": {
                "active_scheme": vulnerability.get("active_scheme"),
                "missingness": vulnerability.get("missingness"),
                "high_correlations": (vulnerability.get("correlation") or {}).get("high_pairs"),
                "vif": vulnerability.get("vif"),
                "weight_comparisons": (vulnerability.get("weight_sensitivity") or {}).get("comparisons"),
            },
            "result_queries": [{
                "metric": answer.get("metric"), "top_rows": answer.get("top_rows"),
                "computation": answer.get("computation"),
            } for answer in query_answers],
            "routing": {
                "router": routing.get("router"), "mobility_scenario": routing.get("mobility_scenario"),
                "fallback": routing.get("fallback"), "accuracy": routing.get("accuracy"),
            },
            "provenance": [{"source": p.source, "tier": p.tier, "record_count": p.record_count}
                           for p in self.ctx.lineage],
        }
        self.ctx.record("explanation", "Requesting an evidence-grounded interpretation from the model.")
        narrative = ResultNarrative(**self.llm.structured(load_prompt("result_interpretation"), json.dumps(evidence)))
        self.ctx.record("explanation", "Generated an evidence-grounded interpretation and reflection.",
                        decision_summary=narrative.decision_summary, self_reflection=narrative.self_reflection)
        return narrative

    def interpret_sensitivity_results(self, task: TaskSpec) -> ResultNarrative:
        """Explain only the confirmed deterministic experiment grid and its provenance."""
        evidence = {
            "task": task.model_dump(mode="json"),
            "sensitivity_results": self.ctx.get("sensitivity_results"),
            "quality": self.ctx.get("quality", {}),
            "vulnerability_analysis": self.ctx.get("vulnerability_analysis", {}),
            "built_environment": self.ctx.get("built_environment", {}),
            "routing": self.ctx.get("travel", {}).get("routing_report", {}),
            "provenance": [item.model_dump(mode="json") for item in self.ctx.lineage],
            "indicators_used": ["population_density", "elderly_ratio", "poverty_rate", "heat_exposure", "environment_burden", "vulnerability_score"],
        }
        self.ctx.record("explanation", "Requesting an evidence-grounded sensitivity interpretation.")
        narrative = ResultNarrative(**self.llm.structured(
            load_prompt("result_interpretation"), json.dumps(evidence)
        ))
        self.ctx.record(
            "explanation", "Generated a provenance-grounded sensitivity interpretation.",
            decision_summary=narrative.decision_summary,
            self_reflection=narrative.self_reflection,
        )
        return narrative

    def interpret_heat_results(self, task: TaskSpec) -> ResultNarrative:
        """Generate a concise interpretation from deterministic heat statistics."""
        heat = self.ctx.get("heat_summary")
        evidence = {"task": task.model_dump(mode="json"), "statistics": heat["statistics"],
                    "top_tracts": heat["top_tracts"], "quality": self.ctx.get("quality", {}),
                    "provenance": [item.model_dump(mode="json") for item in self.ctx.lineage]}
        self.ctx.record("explanation", "Requesting an evidence-grounded heat interpretation from the model.")
        narrative = ResultNarrative(**self.llm.structured(load_prompt("heat_interpretation"), json.dumps(evidence)))
        self.ctx.record("explanation", "Generated a concise heat-pattern interpretation.",
                        decision_summary=narrative.decision_summary, self_reflection=narrative.self_reflection)
        return narrative

    def _completion_message(self, task: TaskSpec) -> str:
        """Return a direct deterministic answer for bounded diagnostic goals."""
        query_parts = []
        if task.result_queries and self.ctx.has("result_query_answers"):
            for answer in self.ctx.get("result_query_answers", {}).get("answers", []):
                rows = answer.get("top_rows", [])
                ranking = ", ".join(
                    f"{row.get('rank')}. tract {row.get('GEOID')} ({float(row.get('value')):.3f})"
                    for row in rows
                )
                if ranking:
                    query_parts.append(
                        f"Top {len(rows)} for {answer.get('metric_label')}: {ranking}."
                    )
        query_suffix = (" " + " ".join(query_parts)) if query_parts else ""
        if task.analysis_goal == "heat_review" and self.ctx.has("heat_summary"):
            summary = self.ctx.get("heat_summary")
            top = summary.get("top_tracts", [])
            if top:
                row = top[0]
                return (
                    f"Tract {row.get('GEOID')} has the highest reported heat "
                    f"exposure in this run at {float(row.get('heat_exposure')):.1f} °C "
                    f"among {summary.get('tracts_reviewed', 'the retained')} tracts. "
                    f"Basis: {summary.get('evidence_basis', 'the recorded Landsat heat-exposure indicator')}"
                )
        if task.analysis_goal == "reachability_review" and self.ctx.has("reachability_analysis"):
            summary = self.ctx.get("reachability_analysis")
            hardest = summary.get("hardest_demand", [])
            if hardest:
                row = hardest[0]
                minutes = row.get("nearest_minutes")
                nearest = "unreachable in the computed matrix" if minutes is None else f"{float(minutes):.1f} minutes from its nearest candidate"
                unserved = len(summary.get("unserved_demand_ids", []))
                return (
                    f"Tract {row.get('demand_id')} is the hardest to reach: it is {nearest}. "
                    f"{unserved} demand unit(s) have no candidate within the confirmed "
                    f"{float(summary.get('threshold_minutes', task.threshold_minutes)):g}-minute threshold. "
                    f"Basis: {summary.get('evidence_basis', 'the recorded nearest-candidate travel matrix')}"
                )
        if task.analysis_goal == "minimum_facilities" and self.ctx.has("minimum_facilities"):
            summary = self.ctx.get("minimum_facilities")
            if summary.get("feasible"):
                return (
                    f"At least {int(summary['minimum_facilities'])} candidate facilities are required "
                    f"to cover {float(summary.get('coverage_target', 1.0)):.0%} of the "
                    f"{str(summary.get('coverage_group', 'all')).replace('_', ' ')} group within "
                    f"{float(summary['threshold_minutes']):g} minutes."
                ) + query_suffix
            threshold = self.ctx.get("minimum_threshold", {})
            if threshold.get("feasible"):
                return (
                    f"No number of the retained candidate facilities can cover "
                    f"{float(summary.get('coverage_target', 1.0)):.0%} of the "
                    f"{str(summary.get('coverage_group', 'all')).replace('_', ' ')} group within "
                    f"{float(summary['threshold_minutes']):g} minutes. With "
                    f"{int(threshold['n_facilities'])} facilities, the smallest computed threshold is "
                    f"{float(threshold['minimum_threshold_minutes']):.1f} minutes by "
                    f"{task.mobility_scenario.replace('_', ' ')}."
                ) + query_suffix
            return (
                "The retained candidate set cannot meet the requested coverage at the confirmed "
                "threshold, and no feasible alternative threshold exists in the computed matrix."
            ) + query_suffix
        if task.analysis_goal == "minimum_threshold" and self.ctx.has("minimum_threshold"):
            summary = self.ctx.get("minimum_threshold")
            if summary.get("feasible"):
                return (
                    f"A threshold of at least {float(summary['minimum_threshold_minutes']):.1f} minutes "
                    f"is required with {int(summary['n_facilities'])} facilities to cover "
                    f"{float(summary.get('coverage_target', 1.0)):.0%} of the "
                    f"{str(summary.get('coverage_group', 'all')).replace('_', ' ')} group."
                )
            return "No feasible threshold was found in the computed travel matrix for the confirmed facility budget."
        if task.result_queries and self.ctx.has("result_query_answers"):
            answers = self.ctx.get("result_query_answers").get("answers", [])
            parts = []
            for answer in answers:
                rows = answer.get("top_rows", [])
                ranking = ", ".join(
                    f"{row.get('rank')}. tract {row.get('GEOID')} ({float(row.get('value')):.3f})"
                    for row in rows
                )
                parts.append(f"Top {len(rows)} for {answer.get('metric_label')}: {ranking}")
            siting = ""
            if task.analysis_goal == "facility_siting" and self.ctx.has("analysis"):
                selected = self.ctx.get("analysis").get("solution").selected_ids
                siting = f"Selected {len(selected)} candidate facilities. "
            return siting + " ".join(parts)
        return f"{task.analysis_goal.replace('_', ' ')} completed."

    def run(self, task: TaskSpec, explain: bool = True) -> dict[str, Any]:
        """Observe results and select one bounded registered action at a time."""
        self.ctx.put("task", task)
        if task.analysis_goal == "tract_review" and not task.result_queries:
            recovery = RecoveryProposal(
                diagnosis="A tract review needs at least one confirmed result metric.",
                question=(
                    "Choose heat exposure, vulnerability, access disadvantage, combined disadvantage, "
                    "or coverage gap before running."
                ),
                suggested_changes={},
            )
            self.ctx.record(
                "hitl", "The run stopped before the agent loop because no tract metric was confirmed.",
                diagnosis=recovery.diagnosis, question=recovery.question, suggested_changes={},
            )
            return {
                "ok": False, "paused": True, "message": recovery.diagnosis,
                "failed_tool": "hitl", "plan": AgentPlan(steps=[], reasoning="No executable tract query."),
                "results": [], "lineage": self.ctx.lineage, "outputs": None, "heat_summary": None,
                "diagnostics": {}, "narrative": None, "recovery": recovery,
                "history": self.ctx.history,
            }
        results: list[ToolResult] = []; steps: list[PlanStep] = []; recovery = None; message = ""
        failed_calls: set[str] = set(); completed_calls: set[str] = set(); repeated_calls: set[str] = set()
        max_steps = self.cfg.get("max_agent_steps", self.cfg.get("llm", {}).get("max_agent_steps", 14))
        goal_handle = self._goal_handle(task); ok, paused, safety_limited = False, False, False
        closure_attempted: set[str] = set()
        for index in range(1, max_steps + 1):
            self.ctx.check_cancelled()
            if self._is_complete(task):
                ok = True; message = self._completion_message(task)
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
                if self._is_complete(task):
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
        if ok:
            self.ctx.progress("Finalizing: interpreting results", 0, 2, unit="stages")
        if ok and explain:
            try:
                if self.ctx.has("analysis") and self.ctx.has("outputs"):
                    narrative = self.interpret_results(task)
                    from oasis.core.outputs.report import append_agent_narrative
                    append_agent_narrative(self.ctx.get("outputs")["report"], narrative)
                elif task.analysis_goal == "sensitivity_analysis" and self.ctx.has("sensitivity_results"):
                    narrative = self.interpret_sensitivity_results(task)
                elif task.analysis_goal == "heat_review" and self.ctx.has("heat_summary"):
                    narrative = self.interpret_heat_results(task)
                    from oasis.core.outputs.report import append_heat_narrative
                    append_heat_narrative(self.ctx.get("heat_summary")["report"], narrative)
            except Exception as exc: log.warning("result interpretation skipped: %s", exc)
        if ok:
            self.ctx.progress("Finalizing: packaging result", 1, 2, unit="stages")
        diagnostic_handles = ["constraint_validation", "minimum_facilities", "minimum_threshold",
                              "reachability_analysis", "scenario_comparison", "sensitivity_results",
                              "vulnerability_analysis", "result_query_answers"]
        diagnostics = {name: json.loads(json.dumps(self.ctx.get(name), default=lambda value: value.model_dump(mode="json")))
                       for name in diagnostic_handles if self.ctx.has(name)}
        plan = AgentPlan(steps=steps, reasoning="Actions were selected from observations one step at a time.")
        failed = next((result.tool for result in reversed(results) if not result.ok), None)
        if ok:
            self.ctx.progress("Run complete", 2, 2, unit="stages")
        return {"ok": ok, "paused": paused, "message": message,
                "failed_tool": None if ok else (failed or ("hitl" if paused else "agent_loop")),
                "plan": plan, "results": results, "lineage": self.ctx.lineage,
                "outputs": self.ctx.get("outputs", None), "heat_summary": self.ctx.get("heat_summary", None),
                "diagnostics": diagnostics, "narrative": narrative, "recovery": recovery,
                "history": self.ctx.history}
