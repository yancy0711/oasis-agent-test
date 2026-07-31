"""Streamlit HITL front end for auditable agent planning and analysis."""
from datetime import date, time
from html import escape
from pathlib import Path
import json
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path: sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st
import streamlit.components.v1 as components
import pandas as pd

from oasis.contracts.domain import TaskSpec, ValueWeights
from oasis.settings import load_cfg, load_profiles
from ui.client import ApiClient, BackendError

WEIGHT_LABELS = {"population_density": "Population density", "elderly_ratio": "Older adults",
                 "poverty_rate": "Poverty", "heat_exposure": "Heat exposure"}
EXAMPLES = {
    "Standard siting": "Site 8 cooling centers in Phoenix, Arizona within a 15-minute walking threshold.",
    "Candidate CSV": "I already have candidate sites in a CSV. Select 8 cooling centers in Phoenix, Arizona from that CSV.",
    "Heat review": "Do not optimize sites. Only show which Phoenix communities have the highest heat exposure.",
    "Constraint test": "The budget allows only 2 cooling centers in Phoenix, but every tract must be reachable within 15 minutes.",
}
PROFILE_LABELS = {"balanced": "Balanced", "efficiency_first": "Efficiency first",
                  "equity_first": "Equity first", "cooling_first": "Cooling first", "custom": "Custom"}
MOBILITY_LABELS = {
    "walking": "Walking", "cycling": "Cycling sensitivity", "driving": "Driving",
    "walk_drive_equity": "Car-access equity: households without a car walk; others drive",
}
TOOL_LABELS = {
    "resolve_study_area": "Resolving the administrative boundary and Census identifiers...",
    "prepare_demand": "Preparing Census, tract, and heat-exposure demand data...",
    "fetch_candidate_sites": "Querying OpenStreetMap candidate facilities...",
    "load_candidate_sites": "Loading and validating the uploaded candidate CSV...",
    "summarize_heat_exposure": "Rendering the tract heat-exposure review...",
    "build_travel_matrix": "Scanning the route cache and requesting missing mobility routes...",
    "optimize_sites": "Solving the fixed-budget MCLP and efficiency-equity scenarios...",
    "validate_constraints": "Checking confirmed global and regional constraints...",
    "find_minimum_facilities": "Solving LSCP for the minimum required facility count...",
    "find_minimum_threshold": "Searching travel times for the minimum feasible threshold...",
    "analyze_reachability": "Identifying hard-to-serve and unreachable tracts...",
    "compare_scenarios": "Comparing the two most recent successful scenarios...",
    "generate_outputs": "Generating the Folium map, Pareto chart, and provenance report...",
}


def _inject_styles() -> None:
    """Apply the warm cartographic visual system without changing behavior."""
    st.markdown("""
    <style>
    .stApp{background:#FAF9F5;color:#263548}.block-container{max-width:1500px;padding:3rem 2rem 3rem}
    [data-testid="stMainBlockContainer"]{padding-top:4rem!important}
    [data-testid="stHeader"]{background:rgba(250,249,245,.88);backdrop-filter:blur(12px)}
    [data-testid="stSidebar"]{background:#F2EEE5;border-right:1px solid #DED8CC}
    [data-testid="stSidebar"] [data-testid="stVerticalBlock"]{gap:.7rem}
    [data-testid="stVerticalBlockBorderWrapper"]{background:rgba(255,255,255,.68);border-color:#DED8CC!important;
      border-radius:16px;box-shadow:0 8px 26px rgba(54,65,78,.055)}
    .oasis-header{display:flex;align-items:center;justify-content:space-between;gap:1rem;padding:.85rem 1.15rem;
      margin:.35rem 0 .85rem;background:rgba(255,255,255,.74);border:1px solid #DED8CC;border-radius:16px}
    .oasis-title{display:flex;align-items:baseline;gap:.75rem;flex-wrap:wrap}.oasis-title b{font-size:1.45rem;color:#172A42}
    .oasis-title span{color:#7A746A}.oasis-badges{display:flex;gap:.55rem;align-items:center}
    .badge{padding:.35rem .75rem;border-radius:999px;font-size:.82rem}.badge-live{color:#12634F;background:#E2F4ED}
    .badge-cache{color:#5D5A55;background:#EEECE7}.badge-sample{color:#82530C;background:#FFF0CE}
    .badge-pending{color:#526273;background:#E8EDF2}.badge-run{color:#574F42;background:#F2EEE5}
    .place-strip{display:flex;align-items:center;gap:.65rem;flex-wrap:wrap;padding:.72rem 1rem;margin:.2rem 0 .9rem;
      background:rgba(255,255,255,.52);border:1px solid #DED8CC;border-radius:14px}
    .place-pill{padding:.34rem .75rem;border-radius:999px;color:#12634F;background:#E2F4ED;font-weight:600}
    .trace-ok{color:#177245}.trace-warn{color:#A66500}.trace-run{color:#185FA5}.trace-queue{color:#928B80}
    .stButton>button{border-radius:10px;font-weight:600;min-height:2.55rem}
    .stButton>button[kind="primary"]{background:#172A42;border-color:#172A42}
    .stButton>button:disabled,.stButton>button[kind="primary"]:disabled{background:#E7E2D8!important;color:#6F6A61!important;border:1px solid #D2CABD!important;opacity:1}
    [data-baseweb="tab-list"]{gap:.35rem;background:#F2EEE5;padding:.28rem;border-radius:11px}
    [data-baseweb="tab"]{border-radius:9px;padding:.55rem .9rem}
    [data-testid="stMetric"]{background:rgba(255,255,255,.62);border:1px solid #E8E2D7;border-radius:13px;padding:.75rem}
    [data-testid="stStatusWidget"]{border-radius:13px;background:rgba(255,255,255,.72)}
    iframe{border-radius:12px}@media(max-width:900px){.block-container{padding:4rem 1rem 1rem}.oasis-header{align-items:flex-start}}
    </style>
    """, unsafe_allow_html=True)


@st.cache_resource
def _api() -> ApiClient:
    """Return one reusable client for the configured OASIS backend."""
    return ApiClient()


@st.cache_data(ttl=30, show_spinner=False)
def _presentation_config() -> tuple[dict, dict, str | None]:
    """Load backend UI defaults, with bundled defaults for offline rendering."""
    try:
        data = _api().config()
        cfg = {"optimization": data.get("optimization", {}),
               "accessibility": data.get("accessibility", {}),
               "execution": data.get("execution", {}),
               "llm": {"max_agent_steps": data.get("agent", {}).get("max_steps", 14)}}
        return cfg, data.get("value_profiles", {}), None
    except BackendError as exc:
        return load_cfg(), load_profiles(), str(exc)


def _candidate_label(candidate: dict) -> str:
    """Return the backend-provided administrative candidate label."""
    return candidate.get("label", f"{candidate.get('canonical_name', 'Unknown')} ({candidate.get('place_type', 'unknown')})")

def _matching_profile(task_data: dict, profiles: dict) -> str | None:
    """Return the registered profile matching parsed weights exactly."""
    weights = task_data.get("weights")
    return next((name for name, profile in profiles.items() if weights == profile["weights"]), None)


def _append_chat(role: str, content: str) -> None:
    """Append one visible conversation message without hidden reasoning."""
    st.session_state.setdefault("chat_history", []).append({"role": role, "content": content})


def _use_example(label: str) -> None:
    """Place one named example directly into the editable request field."""
    st.session_state.prompt_input = EXAMPLES[label]


def _select_example() -> None:
    """Copy the selected example pill into the top prompt field."""
    label = st.session_state.get("example_prompt")
    if label: _use_example(label)


def _apply_profile_choice(profiles: dict) -> None:
    """Copy a registered profile into the editable weight sliders."""
    name = st.session_state.get("profile_choice")
    st.session_state.profile_input = None if name == "custom" else name
    st.session_state.profile_defaulted = False
    if name in profiles:
        for field, value in profiles[name]["weights"].items(): st.session_state[f"weight_{field}"] = float(value)


def _apply_recommended_profile(profile: str) -> None:
    """Queue a profile recommendation for the next pre-widget render boundary."""
    st.session_state.pending_recommended_profile = profile


def _apply_recommended_mobility(mobility: str) -> None:
    """Queue a mobility recommendation without mutating an instantiated widget."""
    st.session_state.pending_recommended_mobility = mobility


def _apply_recommended_sensitivity(thresholds: list, counts: list) -> None:
    """Queue a what-if grid so persisted widget state cannot overwrite it."""
    st.session_state.pending_recommended_sensitivity = (
        [float(value) for value in thresholds], [int(value) for value in counts]
    )


def _apply_quick_preference() -> None:
    """Map one quick clarification answer to explicit custom weights."""
    answer = st.session_state.get("quick_preference")
    presets = {
        "Heat + poverty": {"population_density": .1, "elderly_ratio": .1, "poverty_rate": .4, "heat_exposure": .4},
        "Older adults": {"population_density": .1, "elderly_ratio": .7, "poverty_rate": .1, "heat_exposure": .1},
        "Balanced": {"population_density": .25, "elderly_ratio": .25, "poverty_rate": .25, "heat_exposure": .25},
    }
    if answer not in presets: return
    st.session_state.profile_choice = "custom"; st.session_state.profile_input = None
    for field, value in presets[answer].items(): st.session_state[f"weight_{field}"] = value
    _append_chat("user", answer)


def _apply_recovery_changes() -> None:
    """Apply model-proposed edits before Streamlit recreates keyed widgets."""
    recovery = st.session_state.get("recovery", {}); changes = recovery.get("suggested_changes", {})
    if "n_facilities" in changes: st.session_state.n_input = int(changes["n_facilities"])
    if "threshold_minutes" in changes: st.session_state.threshold_input = float(changes["threshold_minutes"])
    updated = dict(st.session_state.get("task_data", {}))
    for field in ("coverage_target", "candidate_amenities"):
        if field in changes: updated[field] = changes[field]
    st.session_state.task_data = updated; st.session_state.pop("recovery", None)
    _append_chat("user", "Apply the suggested changes and continue with the revised constraints.")


def _dismiss_recovery() -> None:
    """Dismiss a proposal while preserving all editable task controls."""
    st.session_state.pop("recovery", None)
    _append_chat("user", "Keep the confirmed constraints; I will edit them manually.")


def _save_candidate_upload(upload) -> dict:
    """Send one candidate CSV to backend-managed storage."""
    return _api().upload_candidates(upload.name, upload.getvalue())

def _initialize_state(cfg: dict, profiles: dict) -> None:
    """Initialize widget state without overwriting parsed values."""
    optimization = cfg.get("optimization", {}); accessibility = cfg.get("accessibility", {})
    default_profile = optimization.get("value_profile", "balanced")
    if default_profile not in profiles: default_profile = next(iter(profiles))
    legacy_router = st.session_state.get("router_input", accessibility.get("router", "ors"))
    default_mode = cfg.get("execution", {}).get(
        "default_mode", {"haversine": "quick"}.get(legacy_router, "auto")
    )
    defaults = {"prompt_input": "", "n_input": int(optimization.get("n_facilities", 8)),
                "threshold_input": float(accessibility.get("threshold_minutes", 15)),
                "profile_input": default_profile, "profile_choice": default_profile,
                "candidate_source_input": "osm", "mode_input": default_mode,
                "mobility_input": accessibility.get("mobility_scenario", "walking"),
                "service_hours_enabled": False, "service_date_input": date.today(),
                "service_start_input": time(14, 0), "service_end_input": time(18, 0),
                "unknown_hours_input": "include",
                "agent_history": [], "chat_history": [], "mobility_runs": {},
                "completed_runs": [], "sensitivity_thresholds_input": [],
                "sensitivity_facility_counts_input": [], "run_count": 0}
    for key, value in defaults.items(): st.session_state.setdefault(key, value)
    if st.session_state.get("profile_choice") not in [*profiles, "custom"]:
        st.session_state.profile_choice = st.session_state.get("profile_input") or default_profile
    for field, value in profiles[default_profile]["weights"].items():
        st.session_state.setdefault(f"weight_{field}", float(value))


def _data_tier() -> str:
    """Return the most informative provenance tier for the header."""
    tiers = {item.get("tier") for item in st.session_state.get("provenance", []) if isinstance(item, dict)}
    if "sample" in tiers: return "sample"
    if "live" in tiers: return "live"
    if "cache" in tiers: return "cache"
    return "pending"


def _render_header(slot) -> None:
    """Render the title, provenance tier, and run counter."""
    tier = _data_tier(); run = int(st.session_state.get("run_count", 0))
    slot.markdown(f"""
    <div class="oasis-header"><div class="oasis-title"><b>OASIS siting agent</b>
    <span>Cooling-center decision support</span></div><div class="oasis-badges">
    <span class="badge badge-{tier}">data: {tier}</span><span class="badge badge-run">run #{run}</span></div></div>
    """, unsafe_allow_html=True)


def _parse_request(cfg: dict, profiles: dict) -> None:
    """Parse the top prompt through the API and seed editable HITL controls."""
    prompt = st.session_state.prompt_input.strip()
    with st.spinner("Structuring the request with the configured language model..."):
        response = _api().parse(prompt)
    data = response["task"]
    if response.get("missing_place"): st.warning(response.get("message", "A study area is required."))
    st.session_state.agent_history = response.get("events", [])
    st.session_state.task_data = data; st.session_state.place_input = data.get("place_name", "")
    st.session_state.n_input = int(data.get("n_facilities", cfg["optimization"]["n_facilities"]))
    st.session_state.threshold_input = float(data.get("threshold_minutes", cfg["accessibility"]["threshold_minutes"]))
    matched = _matching_profile(data, profiles); default = cfg.get("optimization", {}).get("value_profile", "balanced")
    profile = matched or (default if default in profiles else next(iter(profiles)))
    st.session_state.profile_input = profile; st.session_state.profile_choice = profile
    st.session_state.profile_defaulted = matched is None
    st.session_state.candidate_source_input = data.get("candidate_source", "osm")
    st.session_state.mobility_input = data.get("mobility_scenario", "walking")
    st.session_state.sensitivity_thresholds_input = list(data.get("sensitivity_thresholds", []))
    st.session_state.sensitivity_facility_counts_input = list(data.get("sensitivity_facility_counts", []))
    window = data.get("operating_window")
    st.session_state.service_hours_enabled = bool(window)
    if window:
        st.session_state.service_date_input = date.fromisoformat(window["date"])
        st.session_state.service_start_input = time.fromisoformat(window["start"])
        st.session_state.service_end_input = time.fromisoformat(window["end"])
        st.session_state.unknown_hours_input = window.get("unknown_policy", "include")
    for field, value in profiles[profile]["weights"].items(): st.session_state[f"weight_{field}"] = float(value)
    for key in ("place_candidates", "place_candidates_all", "place_resolution_id", "resolved_place",
                "resolved_level", "resolved_query", "candidate_upload_id", "candidate_upload_name",
                "candidate_upload_key", "recovery", "outputs", "artifact_data", "narrative",
                "heat_summary", "diagnostics", "agent_message", "solution_metrics", "provenance",
                "routing_status", "run_id", "current_mobility_signature"):
        st.session_state.pop(key, None)
    _append_chat("user", prompt)
    summary = f"I parsed this as a {data.get('analysis_goal', 'facility_siting').replace('_', ' ')} request"
    if data.get("place_name"): summary += f" for {data['place_name']}"
    _append_chat("assistant", summary + ". Confirm the cold parameters and study-area boundary before execution.")

def _render_sidebar(cfg: dict, profiles: dict, data: dict) -> dict:
    """Render cold parameters and return validated presentation values."""
    goal = data.get("analysis_goal", "facility_siting"); disabled = goal == "heat_review"
    pending_profile = st.session_state.pop("pending_recommended_profile", None)
    if pending_profile in profiles:
        st.session_state.profile_choice = pending_profile
        _apply_profile_choice(profiles)
    pending_mobility = st.session_state.pop("pending_recommended_mobility", None)
    if pending_mobility in MOBILITY_LABELS:
        st.session_state.mobility_input = pending_mobility
    pending_sensitivity = st.session_state.pop("pending_recommended_sensitivity", None)
    if pending_sensitivity:
        st.session_state.sensitivity_thresholds_input = pending_sensitivity[0]
        st.session_state.sensitivity_facility_counts_input = pending_sensitivity[1]
    with st.sidebar:
        st.header("Parameters"); st.caption("Cold parameters are fixed before each run.")
        n = st.slider("Facilities N", 1, 100, key="n_input", disabled=disabled)
        threshold = st.slider("Threshold (minutes)", 1.0, 60.0, step=1.0, key="threshold_input", disabled=disabled)
        st.radio("Value profile", [*profiles, "custom"], key="profile_choice", disabled=disabled,
                 format_func=lambda name: PROFILE_LABELS.get(name, profiles.get(name, {}).get("label", name)),
                 on_change=_apply_profile_choice, args=(profiles,))
        if st.session_state.get("profile_defaulted"):
            st.caption("No value profile was confirmed by the parser. The visible profile is a UI default; review it before running.")

        custom = st.session_state.profile_choice == "custom"
        with st.expander("Custom objective weights", expanded=custom):
            raw = {field: st.slider(label, 0.0, 1.0, step=.05, key=f"weight_{field}", disabled=disabled or not custom)
                   for field, label in WEIGHT_LABELS.items()}
            total = sum(raw.values()); normalized = ValueWeights(**raw).normalized() if total else None
            st.caption(f"Raw total: {total:.2f} | normalized total: {1.0 if normalized else 0.0:.2f}")
            if normalized:
                st.caption("Normalized: " + " | ".join(f"{field.split('_')[0]} {getattr(normalized, field):.2f}"
                                                       for field in WEIGHT_LABELS))
            if not disabled and abs(total - 1.0) > .001: st.warning("Weights must sum to 1.00 before the agent can run.")
        source = st.radio("Candidate source", ["osm", "csv"], horizontal=True, key="candidate_source_input",
                          disabled=disabled, format_func=lambda value: "OpenStreetMap" if value == "osm" else "Uploaded CSV")
        candidate_upload_id = st.session_state.get("candidate_upload_id")
        if source == "csv" and not disabled:
            sample = Path("data/samples/phoenix_candidate_sites.csv")
            if sample.is_file():
                st.download_button("Download sample CSV", sample.read_bytes(), file_name=sample.name, mime="text/csv")
            upload = st.file_uploader("Candidate CSV", type="csv", help="Use latitude/longitude or lat/lon columns.")
            if upload:
                upload_key = f"{upload.name}:{upload.size}"
                if upload_key != st.session_state.get("candidate_upload_key"):
                    try:
                        saved = _save_candidate_upload(upload)
                        st.session_state.candidate_upload_id = saved["upload_id"]
                        st.session_state.candidate_upload_name = saved["filename"]
                        st.session_state.candidate_upload_key = upload_key
                    except BackendError as exc: st.error(str(exc))
                candidate_upload_id = st.session_state.get("candidate_upload_id")
            if candidate_upload_id: st.success(f"Loaded {st.session_state.get('candidate_upload_name', 'candidate CSV')}")
            else: st.warning("Upload the referenced candidate CSV before running.")
        with st.expander("Advanced", expanded=True):
            recommended_profile = data.get("recommended_value_profile")
            recommended_mobility = data.get("recommended_mobility_scenario")
            rationale = data.get("recommendation_rationale", "")
            if recommended_profile in profiles or recommended_mobility in MOBILITY_LABELS:
                suggestions = []
                if recommended_profile in profiles:
                    suggestions.append(f"value profile: {PROFILE_LABELS.get(recommended_profile, recommended_profile)}")
                if recommended_mobility in MOBILITY_LABELS:
                    suggestions.append(f"mobility: {MOBILITY_LABELS[recommended_mobility]}")
                st.info("LLM suggestion (not applied): " + " | ".join(suggestions))
                if rationale:
                    st.caption(rationale)
                if recommended_profile in profiles:
                    st.button(
                        "Apply suggested value profile",
                        on_click=_apply_recommended_profile,
                        args=(recommended_profile,),
                    )
                if recommended_mobility in MOBILITY_LABELS:
                    st.button(
                        "Apply suggested mobility mode",
                        on_click=_apply_recommended_mobility,
                        args=(recommended_mobility,),
                    )
            mode = st.selectbox(
                "Execution mode", ["auto", "quick", "precise"], key="mode_input",
                format_func=lambda value: {
                    "quick": "Quick preview (Haversine)",
                    "precise": "Precise only (ORS)",
                    "auto": "Auto (quick preview, then ORS)",
                }[value],
            )
            mobility = st.selectbox(
                "Mobility scenario",
                ["walking", "cycling", "driving", "walk_drive_equity"],
                key="mobility_input",
                disabled=disabled,
                format_func=lambda value: MOBILITY_LABELS[value],
            )
            st.caption("The displayed mobility choice becomes executable only when you click Run confirmed agent.")

            if mobility == "walk_drive_equity":
                st.caption(
                    "Households without a car are modeled with walking access; other "
                    "households are modeled with driving access. ACS B08201 is a tract-level "
                    "household proxy, not observed individual behavior; transit is not modeled."
                )
            sensitivity_thresholds = list(st.session_state.get("sensitivity_thresholds_input", []))
            sensitivity_counts = list(st.session_state.get("sensitivity_facility_counts_input", []))
            if goal == "sensitivity_analysis":
                recommended_thresholds = list(data.get("recommended_sensitivity_thresholds", []))
                recommended_counts = list(data.get("recommended_sensitivity_facility_counts", []))
                if recommended_thresholds or recommended_counts:
                    st.info(
                        "LLM what-if suggestion (not applied): "
                        f"thresholds {recommended_thresholds or 'unchanged'}; "
                        f"facility counts {recommended_counts or 'unchanged'}."
                    )
                    st.button(
                        "Apply suggested sensitivity grid",
                        on_click=_apply_recommended_sensitivity,
                        args=(
                            recommended_thresholds or [float(threshold)],
                            recommended_counts or [int(n)],
                        ),
                    )
                threshold_options = sorted(set(
                    [float(value) for value in range(1, 61)] + sensitivity_thresholds
                ))
                count_options = sorted(set(range(1, 101)) | set(sensitivity_counts))
                sensitivity_thresholds = st.multiselect(
                    "Confirmed what-if thresholds (minutes)", threshold_options,
                    key="sensitivity_thresholds_input",
                )
                sensitivity_counts = st.multiselect(
                    "Confirmed what-if facility counts", count_options,
                    key="sensitivity_facility_counts_input",
                )
                st.caption(
                    f"Confirmed grid: {len(sensitivity_thresholds) * len(sensitivity_counts)} "
                    "deterministic solver experiments (maximum 36)."
                )
            service_hours = st.checkbox(
                "Require sites to be available for the full service window",
                key="service_hours_enabled",
                disabled=disabled,
            )
            if service_hours:
                st.caption(
                    "Availability constrains the feasible supply set before the equity "
                    "optimization; it does not recommend the nearest site."
                )
            operating_window = None
            if service_hours:
                service_date = st.date_input("Service date", key="service_date_input")
                service_start = st.time_input("Window start", key="service_start_input")
                service_end = st.time_input("Window end", key="service_end_input")
                unknown_policy = st.selectbox(
                    "Candidates with missing/invalid hours",
                    ["include", "exclude"],
                    key="unknown_hours_input",
                    format_func=lambda value: {
                        "include": "Include and flag as unknown",
                        "exclude": "Exclude from optimization",
                    }[value],
                )
                if service_end <= service_start:
                    st.error("Window end must be after start.")
                else:
                    operating_window = {
                        "date": service_date.isoformat(),
                        "start": service_start.isoformat(timespec="minutes"),
                        "end": service_end.isoformat(timespec="minutes"),
                        "unknown_policy": unknown_policy,
                    }
            routing = st.session_state.get("routing_status", {})
            if routing:
                st.caption(f"Last run: {routing.get('router', 'unknown')}  |  {routing.get('tier', 'unknown')} tier")
                if routing.get("reason"): st.warning(f"Fallback: {routing['reason']}")
            else: st.caption("Precise mode fails visibly if any requested ORS profile is unavailable.")
            if routing.get("mode_population_share"):
                shares = routing["mode_population_share"]
                st.caption("Population proxy: " + " | ".join(
                    f"{name} {float(value):.1%}" for name, value in shares.items()))
            hours = routing.get("operating_hours") or {}
            if hours.get("enabled"):
                st.caption(
                    f"Operating-hours filter: {hours.get('available_candidates', 0)}/"
                    f"{hours.get('potential_candidates', 0)} candidates retained; "
                    f"{hours.get('unknown_candidates', 0)} schedules unknown "
                    f"({hours.get('unknown_policy', 'include')})."
                )
        valid_weights = disabled or bool(normalized and abs(total - 1.0) <= .001)
        return {"n": n, "threshold": threshold, "weights": None if disabled else normalized,
                "mobility": mobility, "operating_window": operating_window,
                "sensitivity_thresholds": sensitivity_thresholds,
                "sensitivity_facility_counts": sensitivity_counts,
                "hours_valid": not service_hours or operating_window is not None,
                "weights_valid": valid_weights, "source": "osm" if disabled else source,
                "candidate_upload_id": None if disabled else candidate_upload_id, "mode": mode}


def _confirm_place(place: str, selected: dict, candidates: list[dict]) -> None:
    """Confirm one opaque backend candidate and store its public identity."""
    confirmed = _api().confirm_place(st.session_state.place_resolution_id, selected["candidate_id"])
    st.session_state.resolved_place = confirmed["canonical_name"]
    st.session_state.resolved_level = confirmed.get("place_type", "boundary")
    st.session_state.resolved_query = place; st.session_state.place_candidates_all = candidates
    st.session_state.pop("place_candidates", None)


def _render_place_strip() -> tuple[str, bool]:
    """Render explicit backend place resolution and ambiguity confirmation."""
    place_col, action_col = st.columns([5, 1], vertical_alignment="bottom")
    with place_col: place = st.text_input("Study area", key="place_input", placeholder="US city or county")
    with action_col: resolve_clicked = st.button("Resolve", disabled=not place.strip(), use_container_width=True)
    if resolve_clicked:
        try:
            response = _api().resolve_place(place)
            candidates = response["candidates"]
            st.session_state.place_resolution_id = response["resolution_id"]
            st.session_state.place_candidates_all = candidates
            if response.get("ambiguous"):
                st.session_state.place_candidates = candidates; st.session_state.pop("resolved_place", None)
            elif response.get("selected"): _confirm_place(place, response["selected"], candidates)
            else: st.error("The backend did not return a usable administrative boundary.")
        except BackendError as exc: st.error(str(exc))
    if "place_candidates" in st.session_state:
        candidates = st.session_state.place_candidates
        st.warning("Multiple valid boundaries were found. Resolution will never be silent.")
        choice = st.radio("Choose the intended administrative area", range(len(candidates)),
                          format_func=lambda index: _candidate_label(candidates[index]))
        if st.button("Confirm candidate", type="primary"):
            try: _confirm_place(place, candidates[choice], candidates); st.rerun()
            except BackendError as exc: st.error(str(exc))
    resolved = st.session_state.get("resolved_place") if st.session_state.get("resolved_query") == place else None
    if resolved:
        level = st.session_state.get("resolved_level", "boundary")
        st.markdown(f'<div class="place-strip">Resolved: <span class="place-pill">'
                    f'{escape(resolved)} ({escape(level)})</span></div>', unsafe_allow_html=True)
        counties = [item for item in st.session_state.get("place_candidates_all", [])
                    if item.get("place_type") == "county" and item.get("canonical_name") != resolved]
        if counties and st.button("Use county instead?", help=_candidate_label(counties[0])):
            try: _confirm_place(place, counties[0], st.session_state.place_candidates_all); st.rerun()
            except BackendError as exc: st.error(str(exc))
    return place, bool(resolved)

def _trace_icon(event: dict) -> tuple[str, str]:
    """Return an ASCII-safe status marker and CSS class for one event."""
    if event.get("stage") == "repair": return "!", "trace-warn"
    if event.get("stage") == "tool_result": return "+", "trace-ok"
    if event.get("stage") in {"tool_call", "decision"}: return ">", "trace-run"
    return "-", "trace-queue"


def _render_trace() -> None:
    """Render a collapsed, compact audit trail instead of a long page section."""
    history = st.session_state.get("agent_history", [])
    durations = st.session_state.get("trace_durations", {})
    with st.expander(f"Run trace ({len(history)} events)", expanded=False):
        st.caption("Observable actions and summaries only; no hidden chain-of-thought.")
        if not history:
            st.info("Parse a request to begin the trace.")
            return
        for event in history:
            icon, css = _trace_icon(event)
            tool = event.get("tool") or event.get("stage", "event")
            duration = durations.get(tool)
            suffix = f" | {duration:.1f}s" if isinstance(duration, (int, float)) else ""
            message = escape(str(event.get("message", "")))
            st.markdown(
                f'<div style="margin:.2rem 0 .55rem"><span class="{css}">{icon}</span> '
                f'<b>{escape(str(tool))}</b>{suffix}<br>'
                f'<span style="color:#6f6a61;font-size:.88rem">{message}</span></div>',
                unsafe_allow_html=True,
            )


def _render_conversation(data: dict) -> None:
    """Render user-visible agent messages and clarification controls."""
    st.subheader("Agent conversation")
    history = st.session_state.get("chat_history", [])
    if not history: st.caption("Describe a request above to start the conversation.")
    for message in history:
        with st.chat_message(message["role"]): st.write(message["content"])
    if data.get("preference_question") and data.get("analysis_goal") != "heat_review":
        with st.chat_message("assistant"): st.write(data["preference_question"])
        st.pills("Quick preference", ["Heat + poverty", "Older adults", "Balanced"], key="quick_preference",
                 selection_mode="single", on_change=_apply_quick_preference, label_visibility="collapsed")
    if "recovery" in st.session_state:
        recovery = st.session_state.recovery
        with st.chat_message("assistant"):
            st.warning(recovery["diagnosis"]); st.write(recovery["question"])
            if recovery.get("suggested_changes"): st.json(recovery["suggested_changes"], expanded=False)
        left, right = st.columns(2)
        left.button("Apply suggested changes", type="primary", use_container_width=True, on_click=_apply_recovery_changes)
        right.button("Keep editing manually", use_container_width=True, on_click=_dismiss_recovery)
    if st.session_state.get("agent_message"):
        with st.chat_message("assistant"): st.write(st.session_state.agent_message)
    if st.session_state.get("narrative"):
        with st.chat_message("assistant"): st.write(st.session_state.narrative["decision_summary"])


TERMINAL_RUN_STATES = {
    "cancelled", "superseded", "interrupted", "succeeded", "awaiting_hitl", "failed"
}


def _reset_run_view() -> None:
    """Clear result-only state while retaining editable inputs and prior trace."""
    for key in ("recovery", "outputs", "artifact_data", "narrative", "heat_summary", "diagnostics",
                "agent_message", "solution_metrics", "routing_status"):
        st.session_state.pop(key, None)


def _apply_run_result(snapshot: dict, result: dict, *, preview: bool) -> None:
    """Hydrate one quick preview or terminal result from public API payloads."""
    for source in ("outputs", "heat_summary", "diagnostics"):
        if result.get(source): st.session_state[source] = result[source]
    if result.get("message") and not result.get("recovery"): st.session_state.agent_message = result["message"]
    if result.get("narrative"): st.session_state.narrative = result["narrative"]
    if result.get("recovery"): st.session_state.recovery = result["recovery"]
    st.session_state.provenance = result.get("lineage", [])
    st.session_state.solution_metrics = result.get("metrics", {})
    st.session_state.execution_provenance = result.get("execution", {})
    if result.get("routing_status"): st.session_state.routing_status = result["routing_status"]
    if not preview:
        _remember_mobility_result(snapshot, result)
        _remember_completed_run(snapshot, result)
    artifacts = {}
    for name, url in result.get("artifacts", {}).items():
        artifact_name = str(url).rstrip("/").rsplit("/", 1)[-1]
        try: artifacts[name] = _api().artifact(snapshot["run_id"], artifact_name)
        except BackendError as exc: st.warning(str(exc))
    st.session_state.artifact_data = artifacts
    error = next((item.get("error") for item in reversed(result.get("results", [])) if item.get("error")), None)
    if not result.get("ok") and error: st.warning(error.get("message", str(error)) if isinstance(error, dict) else str(error))


def _remember_mobility_result(snapshot: dict, result: dict) -> None:
    """Retain comparable site selections from completed single-mode runs in this session."""
    task = dict(snapshot.get("task") or {})
    mobility = task.get("mobility_scenario")
    if not result.get("ok") or mobility not in {"walking", "cycling", "driving"}:
        return
    optimization = next(
        (item.get("summary", {}) for item in result.get("results", [])
         if item.get("tool") == "optimize_sites" and item.get("ok")),
        None,
    )
    if not optimization or not optimization.get("selected_ids"):
        return
    ignored = {
        "mobility_scenario", "recommended_mobility_scenario", "recommended_value_profile",
        "recommendation_rationale", "recommended_sensitivity_thresholds",
        "recommended_sensitivity_facility_counts", "notes",
    }
    comparable = {key: value for key, value in task.items() if key not in ignored}
    signature = json.dumps(comparable, sort_keys=True, separators=(",", ":"), default=str)
    records = optimization.get("selected_sites") or [
        {"cand_id": str(site_id), "name": f"Candidate {site_id}"}
        for site_id in optimization["selected_ids"]
    ]
    histories = dict(st.session_state.get("mobility_runs", {}))
    group = dict(histories.get(signature, {}))
    group[mobility] = {
        "sites": records,
        "coverage": optimization.get("covered_ratio"),
        "vulnerable": optimization.get("vulnerable_covered_ratio"),
        "accuracy": (result.get("execution") or {}).get("accuracy"),
    }
    histories[signature] = group
    st.session_state.mobility_runs = histories
    st.session_state.current_mobility_signature = signature


def _remember_completed_run(snapshot: dict, result: dict) -> None:
    """Keep compact successful run summaries for real cross-run comparison."""
    if not result.get("ok"):
        return
    optimization = next(
        (item.get("summary", {}) for item in result.get("results", [])
         if item.get("tool") == "optimize_sites" and item.get("ok")),
        None,
    )
    if not optimization:
        return
    task = dict(snapshot.get("task") or {})
    record = {
        "run_id": snapshot.get("run_id"),
        "place": task.get("place_name"),
        "mobility": task.get("mobility_scenario", "walking"),
        "n_facilities": task.get("n_facilities"),
        "threshold_minutes": task.get("threshold_minutes"),
        "coverage": optimization.get("covered_ratio"),
        "vulnerable": optimization.get("vulnerable_covered_ratio"),
        "covered_tract_ratio": optimization.get("covered_tract_ratio"),
        "mean_travel_minutes": (result.get("metrics") or {}).get("avg_walk"),
        "selected_ids": [str(value) for value in optimization.get("selected_ids", [])],
        "accuracy": (result.get("execution") or {}).get("accuracy"),
    }
    history = [
        item for item in st.session_state.get("completed_runs", [])
        if item.get("run_id") != record["run_id"]
    ]
    history.append(record)
    st.session_state.completed_runs = history[-12:]


def _start_agent(task: TaskSpec, mode: str, candidate_upload_id: str | None) -> None:
    """Start a run, or atomically supersede active work with revised parameters."""
    _reset_run_view()
    active = st.session_state.get("active_run_id")
    try:
        if active:
            accepted = _api().revise_run(
                active, task.model_dump(mode="json"), mode, candidate_upload_id
            )
        else:
            accepted = _api().start_run(
                task.model_dump(mode="json"), mode, candidate_upload_id
            )
    except BackendError as exc:
        st.error(str(exc))
        return
    st.session_state.active_run_id = accepted["run_id"]
    st.session_state.run_id = accepted["run_id"]
    st.session_state.run_base_history = list(st.session_state.get("agent_history", []))
    st.session_state.run_snapshot = accepted
    st.session_state.pop("hydrated_preview_run", None)


def _eta_label(seconds) -> str:
    """Format a compact ETA without claiming precision before a rate exists."""
    if seconds is None: return "estimating ETA"
    seconds = max(int(seconds), 0)
    return f"ETA {seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"ETA {seconds}s"


def _overall_progress(snapshot: dict) -> tuple[float, str]:
    """Estimate whole-run progress from auditable phase and tool boundaries."""
    status = snapshot.get("status", "queued")
    if status == "succeeded":
        return 1.0, "complete"

    mode = snapshot.get("mode", "auto")
    phase = snapshot.get("phase") or ("quick" if mode in {"quick", "auto"} else "precise")
    events = snapshot.get("events") or []
    phase_start = 0
    for index, event in enumerate(events):
        if event.get("stage") == "hitl" and (event.get("data") or {}).get("phase") == phase:
            phase_start = index
    phase_events = events[phase_start:]
    plan = next((event for event in phase_events if event.get("stage") == "plan"), None)
    steps = ((plan or {}).get("data") or {}).get("steps") or []
    total_tools = max(len(steps), 1)
    completed = min(
        sum(event.get("stage") == "tool_result" for event in phase_events),
        total_tools,
    )
    last_call = max(
        (index for index, event in enumerate(phase_events) if event.get("stage") == "tool_call"),
        default=-1,
    )
    last_result = max(
        (index for index, event in enumerate(phase_events) if event.get("stage") == "tool_result"),
        default=-1,
    )
    current = snapshot.get("progress") or {}
    active_fraction = float(current.get("fraction", 0.05)) if last_call > last_result else 0.0
    phase_fraction = min((completed + max(0.0, min(active_fraction, 1.0))) / total_tools, 0.99)
    if not steps:
        phase_fraction = max(phase_fraction, 0.02 if status == "running" else 0.0)

    if mode == "auto":
        base, span = (0.0, 0.35) if phase == "quick" else (0.35, 0.65)
    else:
        base, span = 0.0, 1.0
    label = "quick preview" if phase == "quick" else "ORS refinement"
    return min(base + span * phase_fraction, 0.99), label


@st.fragment(run_every=0.5)
def _render_active_run() -> None:
    """Poll once per fragment rerun so the rest of the page remains interactive."""
    run_id = st.session_state.get("active_run_id")
    if not run_id:
        return
    try:
        snapshot = _api().get_run(run_id)
    except BackendError as exc:
        st.error(str(exc))
        return

    st.session_state.run_snapshot = snapshot
    events = snapshot.get("events", [])
    st.session_state.agent_history = st.session_state.get("run_base_history", []) + events
    status_name = snapshot.get("status", "queued")
    phase = snapshot.get("phase") or "queued"
    progress = snapshot.get("progress") or {}
    mobility = (snapshot.get("task") or {}).get("mobility_scenario", "walking")
    mobility_label = MOBILITY_LABELS.get(mobility, mobility)
    label = f"{snapshot.get('mode', 'run')}  |  {phase}  |  {status_name}  |  {mobility_label}"
    with st.status(label, expanded=status_name not in TERMINAL_RUN_STATES):
        st.caption(f"Mobility scenario: {mobility_label}")
        overall_fraction, overall_label = _overall_progress(snapshot)
        st.progress(
            overall_fraction,
            text=f"Overall pipeline: {overall_fraction:.0%}  |  {overall_label}",
        )
        if progress and str(progress.get("label", "")).startswith("Waiting for"):
            st.info(
                "The previous run process is being terminated. This revised run starts "
                "as soon as the operating system confirms exit; committed cache blocks are retained."
            )
        elif progress:
            eta_text = _eta_label(progress.get("eta_seconds"))
            solve_limit = progress.get("solve_time_limit_seconds")
            if str(progress.get("label", "")).startswith("Pareto") and solve_limit:
                eta_text += f" | each CBC solve capped at {_eta_label(solve_limit).removeprefix('ETA ')}"
            st.progress(
                progress.get("fraction", 0.0),
                text=f"{progress.get('label', 'Working')}: "
                     f"{progress.get('done', 0)}/{progress.get('total', 0)}  |  "
                     f"{eta_text}",
            )
            if str(progress.get("label", "")).startswith("ORS"):
                st.caption(
                    f"Route cache hits {progress.get('cache_hits', 0)}  |  "
                    f"remote pairs {progress.get('remote_pairs', 0)}  |  "
                    f"in flight {progress.get('inflight_pairs', 0)}"
                )
        elif status_name not in TERMINAL_RUN_STATES:
            st.caption(f"Starting {overall_label}; detailed progress will appear at the next checkpoint.")

        preview = snapshot.get("preview_result")
        if preview and st.session_state.get("hydrated_preview_run") != run_id:
            _apply_run_result(snapshot, preview, preview=True)
            st.session_state.hydrated_preview_run = run_id
            st.rerun()
        if preview and status_name not in TERMINAL_RUN_STATES:
            st.success("Quick approximate result is available; ORS refinement continues.")

        if status_name not in TERMINAL_RUN_STATES:
            if st.button("Cancel current run", key=f"cancel_{run_id}"):
                try: _api().cancel_run(run_id)
                except BackendError as exc: st.error(str(exc))
                st.rerun(scope="fragment")
            return

    result = snapshot.get("result")
    if result:
        _apply_run_result(snapshot, result, preview=False)
    elif status_name in {"cancelled", "superseded", "interrupted"}:
        st.session_state.agent_message = f"Run {status_name}; completed routing blocks remain cached."
    st.session_state.pop("active_run_id", None)
    st.session_state.run_count = int(st.session_state.get("run_count", 0)) + 1
    st.rerun()


def _format_metric(value, percent: bool = False, minutes: bool = False) -> str:
    """Format an optional result for a compact metric card."""
    if value is None: return "--"
    if percent: return f"{float(value):.1%}"
    if minutes: return f"{float(value):.1f} min"
    return str(value)


def _render_sensitivity_results(diagnostics: dict) -> bool:
    """Render deterministic what-if experiments when the current run produced them."""
    sensitivity = diagnostics.get("sensitivity_results", {}) if isinstance(diagnostics, dict) else {}
    experiments = sensitivity.get("experiments", [])
    if not experiments:
        return False
    frame = pd.DataFrame(experiments)
    st.markdown("**Confirmed sensitivity experiments**")
    st.dataframe(frame, hide_index=True, use_container_width=True)
    chart = frame.pivot_table(
        index="threshold_minutes", columns="n_facilities",
        values="covered_population_ratio", aggfunc="first",
    ).sort_index()
    st.line_chart(chart, x_label="Threshold (minutes)", y_label="Population coverage")
    st.caption(
        f"{sensitivity.get('experiment_count', len(frame))} deterministic solutions | "
        f"{MOBILITY_LABELS.get(sensitivity.get('mobility_scenario'), sensitivity.get('mobility_scenario', 'unknown mode'))} | "
        f"router: {sensitivity.get('router', 'unknown')}."
    )
    return True


def _render_mobility_compare() -> None:
    """Compare actual site selections from matching walk, bike, and drive runs."""
    signature = st.session_state.get("current_mobility_signature")
    runs = st.session_state.get("mobility_runs", {}).get(signature, {}) if signature else {}
    if len(runs) < 2:
        st.info(
            "Run the same confirmed parameters with at least two of Walking, Cycling, and "
            "Driving to compare their actual selected sites here. No route-line overlay is used."
        )
        return
    order = [mode for mode in ("walking", "cycling", "driving") if mode in runs]
    labels = {mode: MOBILITY_LABELS[mode].replace(" sensitivity", "") for mode in order}
    site_names = {
        str(site["cand_id"]): site.get("name") or f"Candidate {site['cand_id']}"
        for mode in order for site in runs[mode]["sites"]
    }
    selected = {
        mode: {str(site["cand_id"]) for site in runs[mode]["sites"]}
        for mode in order
    }
    rows = []
    for site_id, name in sorted(site_names.items(), key=lambda item: item[1].lower()):
        rows.append({"Site": name, **{
            labels[mode]: "Selected" if site_id in selected[mode] else "-" for mode in order
        }})
    st.markdown("**Selected-site membership**")
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    overlap = pd.DataFrame(index=[labels[mode] for mode in order],
                           columns=[labels[mode] for mode in order], dtype=float)
    for left in order:
        for right in order:
            union = selected[left] | selected[right]
            overlap.loc[labels[left], labels[right]] = (
                len(selected[left] & selected[right]) / len(union) if union else 1.0
            )
    st.markdown("**Selection overlap (Jaccard)**")
    st.dataframe(
        overlap.style.format("{:.0%}").background_gradient(cmap="Blues", vmin=0, vmax=1),
        use_container_width=True,
    )

    stability = pd.DataFrame([
        {"Site": name, "Modes selected": sum(site_id in selected[mode] for mode in order)}
        for site_id, name in site_names.items()
    ]).sort_values(["Modes selected", "Site"], ascending=[False, True])
    st.markdown("**Site stability across modes**")
    st.bar_chart(stability.set_index("Site")["Modes selected"], horizontal=True)
    metrics = pd.DataFrame([
        {"Mode": labels[mode], "Coverage": runs[mode].get("coverage"),
         "Vulnerable coverage": runs[mode].get("vulnerable"),
         "Accuracy": runs[mode].get("accuracy")}
        for mode in order
    ])
    st.dataframe(metrics, hide_index=True, use_container_width=True)
    st.caption(
        "These are separate optimization solutions under identical non-mobility parameters; "
        "differences therefore show how the travel-time matrix changes the chosen supply set."
    )


def _render_compare(diagnostics: dict) -> None:
    """Compare the two latest successful independent runs from this UI session."""
    runs = list(st.session_state.get("completed_runs", []))
    if len(runs) < 2:
        st.info("Complete two successful siting runs to compare their parameters and outcomes.")
        return
    baseline, alternative = runs[-2], runs[-1]
    left, right = st.columns(2)
    for column, title, values in ((left, "Earlier run", baseline), (right, "Latest run", alternative)):
        with column:
            st.markdown(f"**{title}**")
            st.caption(
                f"{values['place']} | {MOBILITY_LABELS.get(values['mobility'], values['mobility'])} | "
                f"N={values['n_facilities']} | {values['threshold_minutes']} min | "
                f"{values.get('accuracy') or 'unknown accuracy'}"
            )
            st.metric("Coverage", _format_metric(values.get("coverage"), percent=True))
            st.metric("Vulnerable coverage", _format_metric(values.get("vulnerable"), percent=True))
            st.metric("Average travel", _format_metric(values.get("mean_travel_minutes"), minutes=True))
    numeric = ("coverage", "vulnerable", "covered_tract_ratio", "mean_travel_minutes")
    deltas = {
        key: alternative[key] - baseline[key]
        for key in numeric
        if isinstance(alternative.get(key), (int, float)) and isinstance(baseline.get(key), (int, float))
    }
    st.markdown("**Latest - earlier**")
    st.dataframe(pd.DataFrame([deltas]), hide_index=True, use_container_width=True)
    first, second = set(baseline["selected_ids"]), set(alternative["selected_ids"])
    union = first | second
    st.metric("Selected-site overlap", f"{len(first & second)}/{len(union)}")
    comparison = diagnostics.get("scenario_comparison", {}) if isinstance(diagnostics, dict) else {}
    if all(key in comparison for key in ("baseline", "alternative", "delta")):
        with st.expander("Within-run deterministic comparison"):
            st.json(comparison, expanded=False)


def _render_outputs() -> None:
    """Render API artifacts, diagnostics, comparison, report, and metrics."""
    recovery = st.session_state.get("recovery"); diagnostics = st.session_state.get("diagnostics", {})
    facilities = diagnostics.get("minimum_facilities", {}); threshold = diagnostics.get("minimum_threshold", {})
    has_bounds = bool(facilities or threshold); artifacts = st.session_state.get("artifact_data", {})
    execution = st.session_state.get("execution_provenance", {})

    accuracy = execution.get("accuracy")
    if accuracy == "approximate":
        st.warning(
            "Approximate preview - this result and report use Haversine-estimated travel "
            "times. ORS refinement is still running; do not treat this as the final siting result."
        )
    elif accuracy == "network":
        st.success("Precise network result - this result and report use the completed ORS travel-time matrix.")
    if recovery:
        st.error(f"Analysis stopped: {recovery['diagnosis']}"); st.caption(recovery["question"])
    elif has_bounds: st.success("Feasibility diagnostic complete.")
    if recovery or has_bounds:
        left, right = st.columns(2)
        left.metric("Minimum facilities", facilities.get("minimum_facilities") or "Not feasible")
        value = threshold.get("minimum_threshold_minutes")
        right.metric("Minimum threshold", f"{value:.1f} min" if value is not None else "Not calculated")
    heat = st.session_state.get("heat_summary", {})
    map_tab, pareto_tab, compare_tab, report_tab = st.tabs(["Map", "Pareto", "Compare", "Report"])
    with map_tab:
        if artifacts.get("map"): components.html(artifacts["map"].decode("utf-8"), height=650)
        elif recovery: st.warning("No solution map was generated because the confirmed constraints are infeasible or the run was interrupted.")
        else: st.info("The interactive map will appear after a successful spatial analysis.")
        if heat.get("top_tracts"): st.dataframe(heat["top_tracts"], use_container_width=True)
    with pareto_tab:
        if artifacts.get("pareto"): st.image(artifacts["pareto"], use_container_width=True)
        else: st.info("The Pareto frontier is produced for a successful facility-siting run.")
    with compare_tab:
        if _render_sensitivity_results(diagnostics):
            st.divider()
        _render_mobility_compare()
        st.divider()
        _render_compare(diagnostics)
    with report_tab:
        if artifacts.get("heat_analysis"):
            st.image(artifacts["heat_analysis"], caption="Heat distribution and tract spatial autocorrelation", use_container_width=True)
        if artifacts.get("report"):
            text = artifacts["report"].decode("utf-8"); st.markdown(text)
            phase = execution.get("phase") or "result"
            st.download_button(
                "Download report", text, file_name=f"oasis_report_{phase}.md", mime="text/markdown"
            )
        elif recovery:
            st.markdown(f"### Feasibility diagnostic\n\n{recovery['diagnosis']}\n\n**Next decision:** {recovery['question']}")
        elif st.session_state.get("narrative"):
            st.markdown(st.session_state.narrative["decision_summary"])
            st.markdown(st.session_state.narrative["self_reflection"])
        else: st.info("The provenance-backed report will appear after output generation.")
    metrics = st.session_state.get("solution_metrics", {})
    coverage, vulnerable, walk = st.columns(3)
    coverage.metric("Coverage", _format_metric(metrics.get("coverage"), percent=True))
    vulnerable.metric("Vulnerable", _format_metric(metrics.get("vulnerable"), percent=True))
    walk.metric("Avg travel", _format_metric(metrics.get("avg_walk"), minutes=True))
    st.caption("Blue denotes supply and selected solutions; orange-red denotes demand and heat exposure.")

st.set_page_config(page_title="OASIS siting agent", layout="wide", page_icon="O")
_inject_styles(); cfg, profiles, bootstrap_error = _presentation_config(); _initialize_state(cfg, profiles)
header_slot = st.empty(); _render_header(header_slot)
if bootstrap_error: st.caption("Backend config unavailable; using bundled display defaults. Start FastAPI before parsing or running.")

with st.container(border=True):
    st.pills("Example requests", list(EXAMPLES), key="example_prompt", selection_mode="single",
             on_change=_select_example, label_visibility="collapsed")
    prompt_col, parse_col = st.columns([7, 1])
    with prompt_col:
        st.text_input("Natural-language request", key="prompt_input",
                      placeholder="Describe where, how many facilities, the access threshold, and who should be prioritized.",
                      label_visibility="collapsed")
    with parse_col:
        parse_clicked = st.button("Analyze request", type="primary",
                                  disabled=not st.session_state.prompt_input.strip(), use_container_width=True)
    with st.expander("Example test prompts"):
        for index, (label, example) in enumerate(EXAMPLES.items()):
            st.markdown(f"**{label}**"); st.code(example, language=None)
            st.button("Use this prompt", key=f"use_example_{index}", on_click=_use_example, args=(label,))
    if parse_clicked:
        try: _parse_request(cfg, profiles)
        except Exception as exc: st.error(f"Could not parse the request: {exc}")

data = st.session_state.get("task_data", {})
controls = _render_sidebar(cfg, profiles, data)
if not data:
    st.info("Start with a natural-language request. The model will propose a typed task before any geospatial tool runs.")
    _render_header(header_slot); st.stop()

goal = data.get("analysis_goal", "facility_siting")
if data.get("coverage_target") is not None:
    st.warning(f"Hard coverage requirement detected: {float(data['coverage_target']):.0%} of tracts within "
               "the confirmed threshold. The agent will pause for HITL if the solved plan falls short.")
place, resolved = _render_place_strip()
candidate_ready = controls["source"] == "osm" or bool(controls["candidate_upload_id"])
sensitivity_ready = (
    goal != "sensitivity_analysis" or (
        bool(controls["sensitivity_thresholds"])
        and bool(controls["sensitivity_facility_counts"])
        and len(controls["sensitivity_thresholds"]) * len(controls["sensitivity_facility_counts"]) <= 36
    )
)
ready = bool(resolved and (goal == "heat_review" or (
    controls["weights_valid"] and controls["hours_valid"] and candidate_ready
    and sensitivity_ready
)))
blockers = []
if not resolved: blockers.append("resolve and confirm the study area")
if goal != "heat_review" and not controls["weights_valid"]: blockers.append("make the four weights sum to 1.00")
if goal != "heat_review" and not controls["hours_valid"]: blockers.append("make the operating-window end later than its start")
if goal != "heat_review" and not candidate_ready: blockers.append("upload a valid candidate CSV")
if not sensitivity_ready: blockers.append("confirm a non-empty sensitivity grid of at most 36 experiments")
if blockers: st.caption("Before running: " + "; ".join(blockers) + ".")

run_col, note_col = st.columns([1, 4], vertical_alignment="center")
active_run = bool(st.session_state.get("active_run_id"))
with run_col:
    run_clicked = st.button("Run revised parameters" if active_run else "Run confirmed agent",
                            type="primary", disabled=not ready, use_container_width=True)
with note_col:
    st.caption("Controls remain editable during a run; revised parameters create an immutable child run.")

if run_clicked:
    task = TaskSpec(place_name=st.session_state.resolved_place,
                    intervention=data.get("intervention", "cooling_center"), analysis_goal=goal,
                    candidate_source=controls["source"], candidate_file=None,
                    mobility_scenario=controls["mobility"],
                    recommended_mobility_scenario=data.get("recommended_mobility_scenario"),
                    recommended_value_profile=data.get("recommended_value_profile"),
                    recommendation_rationale=data.get("recommendation_rationale", ""),
                    operating_window=controls["operating_window"],
                    n_facilities=int(controls["n"]), threshold_minutes=float(controls["threshold"]),
                    coverage_target=data.get("coverage_target"), coverage_group=data.get("coverage_group", "all"),
                    weights=controls["weights"],
                    candidate_amenities=data.get("candidate_amenities",
                                                 ["library", "community_centre", "sports_centre", "arts_centre"]),
                    regional_requirements=data.get("regional_requirements", []),
                    sensitivity_thresholds=controls["sensitivity_thresholds"],
                    sensitivity_facility_counts=controls["sensitivity_facility_counts"],
                    recommended_sensitivity_thresholds=data.get("recommended_sensitivity_thresholds", []),
                    recommended_sensitivity_facility_counts=data.get("recommended_sensitivity_facility_counts", []),
                    preference_question=data.get("preference_question", ""),
                    notes=f"{data.get('notes', '')} [Streamlit HITL confirmed]".strip())
    _start_agent(task, controls["mode"], controls["candidate_upload_id"])

_render_active_run()

left, right = st.columns([.37, .63], gap="large")
with left:
    with st.container(border=True): _render_conversation(data)
    with st.container(border=True):
        _render_trace()
with right:
    with st.container(border=True): _render_outputs()
