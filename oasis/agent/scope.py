"""Deterministic scope routing before any geospatial work starts."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

Disposition = Literal["full_analysis", "quick_diagnostic", "needs_clarification", "out_of_scope"]


def route_origin_from_prompt(prompt: str) -> str | None:
    """Return an explicitly named origin for a nearest-facility route request."""
    text = prompt.strip()
    route_intent = re.search(
        r"\b(?:nearest|closest|route|directions?|how\s+(?:can|do)\s+i\s+(?:find|get|reach))\b",
        text, re.I,
    ) and re.search(r"\b(?:cooling|facility|facilities|cent(?:er|re)|site)\b", text, re.I)
    if not route_intent:
        return None
    patterns = (
        r"\b(?:i(?:'m|\s+am)\s+(?:in|at)|starting\s+(?:from|at)|from)\s+(.+?)"
        r"(?=,\s*(?:how|where|what)|\s+(?:how|where|what|and\s+find|to\s+(?:the\s+)?nearest)|[?.!]|$)",
        r"\b(?:nearest|closest)\s+(?:cooling\s+)?(?:cent(?:er|re)|facility|site)\s+(?:from|to)\s+(.+?)(?=[?.!]|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            value = match.group(1).strip(" ,")
            conceptual = re.fullmatch(
                r"(?:a|the)?\s*(?:policy|planning|research|equity|academic|general)\s+"
                r"(?:perspective|standpoint|point\s+of\s+view)",
                value,
                re.I,
            )
            if value and not conceptual:
                return value
    return None


@dataclass(frozen=True)
class ScopeDecision:
    """One auditable request boundary decision with a useful next step."""

    disposition: Disposition
    reason: str
    alternative: str = ""


_OUT_OF_SCOPE = (
    (r"\bshould\s+(?:we|the\s+city|the\s+county)\s+"
     r"(?:build|open|add|construct)\s+(?:more\s+)?cooling\s+"
     r"(?:centers?|centres?|facilities)\b|"
     r"(?:\u6211\u4eec|\u8be5\u5e02|\u57ce\u5e02).{0,8}"
     r"(?:\u5e94\u8be5|\u662f\u5426\u9700\u8981).{0,8}"
     r"(?:\u65b0\u5efa|\u589e\u5efa|\u5efa\u8bbe|\u589e\u52a0).{0,8}"
     r"(?:\u964d\u6e29\u4e2d\u5fc3|\u907f\u6691\u4e2d\u5fc3)",
     "OASIS cannot make an open-ended normative decision about whether a city should build more facilities.",
     "Ask for a measurable diagnostic, for example the minimum candidate-facility count needed to cover everyone within 15 minutes.", "always"),
    (r"\bopen\s+right\s+now\b",
     "OASIS does not verify real-time facility operations.",
     "Check the city or county cooling-resource directory or call the local 311 service.", "always"),
    (r"\b(heatstroke|fainting|medical emergency|call an ambulance)\b|\u4e2d\u6691|\u6025\u6551|\u660f\u5012",
     "OASIS is not an emergency or medical-advice service.",
     "Contact local emergency services or a qualified health provider now.", "always"),
    (r"\b(open now|currently open|live status|real[- ]time occupancy|current wait)\b|\u73b0\u5728\u5f00\u653e|\u5b9e\u65f6\u72b6\u6001|\u5b9e\u65f6\u4eba\u6570",
     "OASIS does not verify real-time facility operations.",
     "Check the city or county cooling-resource directory or call the local 311 service.", "always"),
    (r"\b(bus|subway|metro|public transit|train timetable)\b|\u516c\u4ea4|\u5730\u94c1|\u516c\u5171\u4ea4\u901a",
     "Public-transport schedules and fares are outside the current walking, cycling, and driving model.",
     "Use the local transit agency trip planner, then use OASIS for the policy-level accessibility analysis.", "primary"),
    (r"\b(zoning|parcel|vacant land|empty lot|buy land|construction permit)\b|\u5206\u533a|\u5730\u5757|\u7a7a\u5730|\u5efa\u8bbe\u8bb8\u53ef",
     "OASIS selects from existing candidate facilities; it does not perform parcel or zoning suitability analysis.",
     "Use the city or county parcel and zoning GIS, then upload approved candidate coordinates to OASIS.", "primary"),
)


def _negated_or_background(text: str, match: re.Match[str]) -> bool:
    """Recognize a bounded local negation/constraint context without parsing the whole request."""
    before = text[max(0, match.start() - 55):match.start()]
    return bool(re.search(
        r"\b(?:not|without|exclude|excluding|avoid|avoiding|away\s+from|do\s+not|don't|no\s+need\s+for)\b[^.!?;]*$",
        before, re.I,
    ))


def _is_primary_unsupported_request(text: str, match: re.Match[str]) -> bool:
    """Hard-reject only when an unsupported capability is the request's clear object."""
    if _negated_or_background(text, match):
        return False
    before = text[max(0, match.start() - 70):match.start()]
    direct_request = re.search(
        r"\b(?:find|show|check|provide|search|analy[sz]e|evaluate|perform|conduct|plan|calculate)"
        r"(?:\s+me)?(?:\s+the|\s+for)?\s*$",
        before, re.I,
    )
    question = re.match(r"\s*(?:what|which|where|when|how|is|are|can|does|do)\b", text, re.I)
    return bool(direct_request or (question and match.start() <= 90))


def classify_request(prompt: str) -> ScopeDecision:
    """Classify only clear boundaries; ambiguous planning requests continue to the parser."""
    text = prompt.strip()
    route_origin = route_origin_from_prompt(text)
    if route_origin and re.fullmatch(
        r"(?:the\s+)?(?:national\s+)?park(?:'s)?\s+(?:main\s+)?(?:gate|entrance)",
        route_origin,
        re.I,
    ):
        return ScopeDecision(
            "needs_clarification",
            "The route request names a generic park entrance, not an identifiable origin.",
            "Name the park and, when possible, the entrance, city, state, or street address.",
        )
    if len(text.split()) < 2 and len(text) < 8:
        return ScopeDecision(
            "needs_clarification", "The request is too short to identify a planning question.",
            "State a US city or county and the cooling-access question you want to evaluate.",
        )
    if re.fullmatch(r"(?:please\s+)?(?:help me|help me improve cooling access|improve cooling access|plan cooling access)[.!?\s]*", text, re.I):
        return ScopeDecision(
            "needs_clarification", "The request does not yet identify a study area or a specific planning question.",
            "Add a US city or county and say whether you want candidate selection, heat review, reachability, or an equity comparison.",
        )
    for pattern, reason, alternative, policy in _OUT_OF_SCOPE:
        match = re.search(pattern, text, re.I)
        if match and not _negated_or_background(text, match) and (
            policy == "always" or _is_primary_unsupported_request(text, match)
        ):
            return ScopeDecision("out_of_scope", reason, alternative)
    return ScopeDecision("full_analysis", "The request is within the supported planning scope.")
