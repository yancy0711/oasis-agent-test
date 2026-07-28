"""Streamlit HITL front end for auditable agent planning and analysis."""
from html import escape
from pathlib import Path
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path: sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st
import streamlit.components.v1 as components

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
TOOL_LABELS = {
    "resolve_study_area": "Resolving the administrative boundary and Census identifiers...",
    "prepare_demand": "Preparing Census, tract, and heat-exposure demand data...",
    "fetch_candidate_sites": "Querying OpenStreetMap candidate facilities...",
    "load_candidate_sites": "Loading and validating the uploaded candidate CSV...",
    "summarize_heat_exposure": "Rendering the tract heat-exposure review...",
    "build_travel_matrix": "Scanning the ORS cache and requesting missing walking routes...",
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
    if name in profiles:
        for field, value in profiles[name]["weights"].items(): st.session_state[f"weight_{field}"] = float(value)


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
    defaults = {"prompt_input": "", "n_input": int(optimization.get("n_facilities", 8)),
                "threshold_input": float(accessibility.get("threshold_minutes", 15)),
                "profile_input": default_profile, "profile_choice": default_profile,
                "candidate_source_input": "osm", "router_input": accessibility.get("router", "ors"),
                "agent_history": [], "chat_history": [], "run_count": 0}
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
    <div class="oasis-header"><div class="oasis-title"><b>◉ OASIS siting agent</b>
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
    for field, value in profiles[profile]["weights"].items(): st.session_state[f"weight_{field}"] = float(value)
    for key in ("place_candidates", "place_candidates_all", "place_resolution_id", "resolved_place",
                "resolved_level", "resolved_query", "candidate_upload_id", "candidate_upload_name",
                "candidate_upload_key", "recovery", "outputs", "artifact_data", "narrative",
                "heat_summary", "diagnostics", "agent_message", "solution_metrics", "provenance",
                "routing_status", "run_id"):
        st.session_state.pop(key, None)
    _append_chat("user", prompt)
    summary = f"I parsed this as a {data.get('analysis_goal', 'facility_siting').replace('_', ' ')} request"
    if data.get("place_name"): summary += f" for {data['place_name']}"
    _append_chat("assistant", summary + ". Confirm the cold parameters and study-area boundary before execution.")

def _render_sidebar(cfg: dict, profiles: dict, data: dict) -> dict:
    """Render cold parameters and return validated presentation values."""
    goal = data.get("analysis_goal", "facility_siting"); disabled = goal == "heat_review"
    with st.sidebar:
        st.header("Parameters"); st.caption("Cold parameters are fixed before each run.")
        n = st.slider("Facilities N", 1, 100, key="n_input", disabled=disabled)
        threshold = st.slider("Threshold (minutes)", 1.0, 60.0, step=1.0, key="threshold_input", disabled=disabled)
        st.radio("Value profile", [*profiles, "custom"], key="profile_choice", disabled=disabled,
                 format_func=lambda name: PROFILE_LABELS.get(name, profiles.get(name, {}).get("label", name)),
                 on_change=_apply_profile_choice, args=(profiles,))
        custom = st.session_state.profile_choice == "custom"
        with st.expander("Custom objective weights", expanded=custom):
            raw = {field: st.slider(label, 0.0, 1.0, step=.05, key=f"weight_{field}", disabled=disabled or not custom)
                   for field, label in WEIGHT_LABELS.items()}
            total = sum(raw.values()); normalized = ValueWeights(**raw).normalized() if total else None
            st.caption(f"Raw total: {total:.2f} · normalized total: {1.0 if normalized else 0.0:.2f}")
            if normalized:
                st.caption("Normalized: " + " · ".join(f"{field.split('_')[0]} {getattr(normalized, field):.2f}"
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
        with st.expander("Advanced"):
            router = st.selectbox("Travel router", ["ors", "haversine"], key="router_input",
                                  format_func=lambda value: "OpenRouteService (ORS)" if value == "ors" else "Haversine approximation")
            routing = st.session_state.get("routing_status", {})
            if routing:
                st.caption(f"Last run: {routing.get('router', 'unknown')} · {routing.get('tier', 'unknown')} tier")
                if routing.get("reason"): st.warning(f"Fallback: {routing['reason']}")
            else: st.caption("Fallback status: not used yet")
        valid_weights = disabled or bool(normalized and abs(total - 1.0) <= .001)
        return {"n": n, "threshold": threshold, "weights": None if disabled else normalized,
                "weights_valid": valid_weights, "source": "osm" if disabled else source,
                "candidate_upload_id": None if disabled else candidate_upload_id, "router": router}


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
        st.markdown(f'<div class="place-strip">⌖ Resolved: <span class="place-pill">'
                    f'{escape(resolved)} ({escape(level)})</span></div>', unsafe_allow_html=True)
        counties = [item for item in st.session_state.get("place_candidates_all", [])
                    if item.get("place_type") == "county" and item.get("canonical_name") != resolved]
        if counties and st.button("Use county instead?", help=_candidate_label(counties[0])):
            try: _confirm_place(place, counties[0], st.session_state.place_candidates_all); st.rerun()
            except BackendError as exc: st.error(str(exc))
    return place, bool(resolved)

def _trace_icon(event: dict) -> tuple[str, str]:
    """Return a visible icon and CSS class for one event."""
    if event.get("stage") == "repair": return "⚠", "trace-warn"
    if event.get("stage") == "tool_result": return "✓", "trace-ok"
    if event.get("stage") in {"tool_call", "decision"}: return "◌", "trace-run"
    return "·", "trace-queue"


def _render_trace() -> None:
    """Render structured actions and visible self-correction events."""
    history = st.session_state.get("agent_history", []); durations = st.session_state.get("trace_durations", {})
    st.subheader("Run trace"); st.caption("Observable actions and summaries only; no hidden chain-of-thought.")
    if not history: st.info("Parse a request to begin the trace."); return
    for index, event in enumerate(history, 1):
        icon, css = _trace_icon(event); tool = event.get("tool") or event.get("stage", "event")
        duration = durations.get(tool); suffix = f" · {duration:.1f}s" if isinstance(duration, (int, float)) else ""
        st.markdown(f'<span class="{css}">{icon}</span> **{escape(str(tool))}**{suffix}', unsafe_allow_html=True)
        if event.get("stage") == "repair": st.warning(event.get("message", "Self-correction applied."))
        elif event.get("stage") in {"observation", "hitl"}: st.caption(event.get("message", ""))
        if event.get("data") or event.get("stage") == "tool_result":
            with st.expander(f"Details {index}"):
                st.write(event.get("message", ""))
                if event.get("data"): st.json(event["data"], expanded=False)


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


def _run_agent(cfg: dict, task: TaskSpec, router: str, candidate_upload_id: str | None = None) -> dict:
    """Queue an API run and stream its public events and deterministic progress."""
    for key in ("recovery", "outputs", "artifact_data", "narrative", "heat_summary", "diagnostics",
                "agent_message", "trace_durations", "solution_metrics"):
        st.session_state.pop(key, None)
    timers, durations = {}, {}; base_history = list(st.session_state.get("agent_history", []))
    snapshot = None
    with st.status("The backend is queueing the confirmed request...", expanded=True) as status:
        detail = st.empty(); overall = st.progress(0.0, text="Planning registered tools")
        operation = st.progress(0.0, text="Waiting for the first observable operation")
        state = {"decision": 0, "max_actions": cfg.get("llm", {}).get("max_agent_steps", 14)}

        def show_event(event: dict) -> None:
            """Update visible status from one public agent event."""
            stage, data, tool = event.get("stage"), event.get("data", {}), event.get("tool")
            if stage == "plan" and data.get("steps"):
                detail.write("Plan: " + " → ".join(step.get("tool", "action") for step in data["steps"]))
            elif stage == "decision":
                state["decision"] = data.get("step_index", 1)
                status.update(label=f"Agent decision {state['decision']}: {tool or data.get('kind', 'action')}")
                overall.progress(min(state["decision"] / state["max_actions"], 1.0),
                                 text=f"Observation-action step {state['decision']}/{state['max_actions']}")
                detail.info(event.get("message", ""))
            elif stage == "tool_call":
                timers[tool] = time.perf_counter(); status.update(label=TOOL_LABELS.get(tool, f"Running {tool}..."))
                if tool == "build_travel_matrix":
                    detail.info("Scanning ORS cache entries and requesting only missing routes. The counter below is exact.")
                elif tool == "optimize_sites":
                    detail.info("Solving MCLP with the confirmed budget, threshold, and value weights.")
                operation.progress(0.0, text="Waiting for deterministic tool progress")
            elif stage == "tool_result":
                if tool in timers: durations[tool] = time.perf_counter() - timers[tool]
                detail.write(event.get("message", ""))
            elif stage == "observation":
                status.update(label="The agent found an actionable issue and is evaluating recovery...")
                detail.warning(event.get("message", ""))
            elif stage == "repair":
                status.update(label=f"Self-correcting {tool} with revised arguments...")
                detail.warning(f"Repair action: {event.get('message', '')}")
            elif stage == "hitl":
                status.update(label="Preparing a human-confirmable recovery proposal..."); detail.info(event.get("message", ""))
            elif stage == "explanation":
                status.update(label="Generating the evidence-grounded interpretation..."); detail.write(event.get("message", ""))

        try:
            accepted = _api().start_run(task.model_dump(mode="json"), router, candidate_upload_id)
            run_id = accepted["run_id"]; st.session_state.run_id = run_id
            seen, last_progress = 0, None
            while True:
                snapshot = _api().get_run(run_id)
                events = snapshot.get("events", [])
                for event in events[seen:]: show_event(event)
                seen = len(events)
                progress = snapshot.get("progress")
                if progress and progress != last_progress:
                    operation.progress(min(progress["done"] / max(progress["total"], 1), 1.0),
                                       text=f"{progress['label']}: {progress['done']}/{progress['total']}")
                    last_progress = progress
                if snapshot.get("status") in {"succeeded", "awaiting_hitl", "failed"}: break
                time.sleep(.25)
            result = snapshot.get("result") or {"ok": False, "failed_tool": "backend", "results": [],
                                                "message": snapshot.get("error", "The backend returned no result."),
                                                "recovery": None, "artifacts": {}}
        except BackendError as exc:
            recovery = {"diagnosis": str(exc),
                        "question": "Start or inspect the FastAPI backend, then retry this confirmed request.",
                        "suggested_changes": {}}
            result = {"ok": False, "failed_tool": "backend", "message": str(exc), "outputs": None,
                      "recovery": recovery, "results": [], "lineage": [], "metrics": {}, "artifacts": {}}
            st.error(str(exc))
        status.update(label="Agent run complete" if result.get("ok") else
                      f"Paused for review at {result.get('failed_tool', 'backend')}",
                      state="complete" if result.get("ok") else "error")
    events = snapshot.get("events", []) if snapshot else []
    st.session_state.trace_durations = durations
    st.session_state.agent_history = base_history + events
    st.session_state.run_count += 1
    for source in ("outputs", "heat_summary", "diagnostics"):
        if result.get(source): st.session_state[source] = result[source]
    if result.get("message") and not result.get("recovery"): st.session_state.agent_message = result["message"]
    if result.get("narrative"): st.session_state.narrative = result["narrative"]
    if result.get("recovery"): st.session_state.recovery = result["recovery"]
    st.session_state.provenance = result.get("lineage", [])
    st.session_state.solution_metrics = result.get("metrics", {})
    if result.get("routing_status"): st.session_state.routing_status = result["routing_status"]
    artifacts = {}
    if snapshot:
        for name in result.get("artifacts", {}):
            try: artifacts[name] = _api().artifact(snapshot["run_id"], name)
            except BackendError as exc: st.warning(str(exc))
    st.session_state.artifact_data = artifacts
    error = next((item.get("error") for item in reversed(result.get("results", [])) if item.get("error")), None)
    if not result.get("ok") and error: st.warning(error.get("message", str(error)) if isinstance(error, dict) else str(error))
    return result

def _format_metric(value, percent: bool = False, minutes: bool = False) -> str:
    """Format an optional result for a compact metric card."""
    if value is None: return "—"
    if percent: return f"{float(value):.1%}"
    if minutes: return f"{float(value):.1f} min"
    return str(value)


def _render_compare(diagnostics: dict) -> None:
    """Render the latest deterministic scenario comparison."""
    comparison = diagnostics.get("scenario_comparison", diagnostics) if isinstance(diagnostics, dict) else {}
    if not all(key in comparison for key in ("baseline", "alternative", "delta")):
        st.info("Run and compare two successful scenarios to populate this view.")
        if diagnostics: st.json(diagnostics, expanded=False)
        return
    left, right = st.columns(2)
    for column, title, values in ((left, "Baseline", comparison["baseline"]),
                                  (right, "Alternative", comparison["alternative"])):
        with column: st.markdown(f"**{title}**"); st.json(values, expanded=False)
    st.markdown("**Alternative − baseline**"); st.json(comparison["delta"], expanded=False)


def _render_outputs() -> None:
    """Render API artifacts, diagnostics, comparison, report, and metrics."""
    recovery = st.session_state.get("recovery"); diagnostics = st.session_state.get("diagnostics", {})
    facilities = diagnostics.get("minimum_facilities", {}); threshold = diagnostics.get("minimum_threshold", {})
    has_bounds = bool(facilities or threshold); artifacts = st.session_state.get("artifact_data", {})
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
    with compare_tab: _render_compare(diagnostics)
    with report_tab:
        if artifacts.get("heat_analysis"):
            st.image(artifacts["heat_analysis"], caption="Heat distribution and tract spatial autocorrelation", use_container_width=True)
        if artifacts.get("report"):
            text = artifacts["report"].decode("utf-8"); st.markdown(text)
            st.download_button("Download report", text, file_name="oasis_report.md", mime="text/markdown")
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
    walk.metric("Avg walk", _format_metric(metrics.get("avg_walk"), minutes=True))
    st.caption("Blue denotes supply and selected solutions; orange-red denotes demand and heat exposure.")

st.set_page_config(page_title="OASIS siting agent", layout="wide", page_icon="◎")
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
        parse_clicked = st.button("Parse intent", type="primary",
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
ready = bool(resolved and (goal == "heat_review" or (controls["weights_valid"] and candidate_ready)))
blockers = []
if not resolved: blockers.append("resolve and confirm the study area")
if goal != "heat_review" and not controls["weights_valid"]: blockers.append("make the four weights sum to 1.00")
if goal != "heat_review" and not candidate_ready: blockers.append("upload a valid candidate CSV")
if blockers: st.caption("Before running: " + "; ".join(blockers) + ".")

run_col, note_col = st.columns([1, 4])
with run_col:
    run_clicked = st.button("Run confirmed agent", type="primary", disabled=not ready, use_container_width=True)
with note_col:
    st.caption("The model selects registered actions from observations; confirmed constraints are never changed silently.")

left, right = st.columns([.37, .63], gap="large")
with left:
    with st.container(border=True): _render_conversation(data)
    with st.container(border=True):
        if run_clicked:
            task = TaskSpec(place_name=st.session_state.resolved_place,
                            intervention=data.get("intervention", "cooling_center"), analysis_goal=goal,
                            candidate_source=controls["source"], candidate_file=None,
                            n_facilities=int(controls["n"]), threshold_minutes=float(controls["threshold"]),
                            coverage_target=data.get("coverage_target"), coverage_group=data.get("coverage_group", "all"),
                            weights=controls["weights"],
                            candidate_amenities=data.get("candidate_amenities",
                                                         ["library", "community_centre", "school"]),
                            regional_requirements=data.get("regional_requirements", []),
                            preference_question=data.get("preference_question", ""),
                            notes=f"{data.get('notes', '')} [Streamlit HITL confirmed]".strip())
            _run_agent(cfg, task, controls["router"], controls["candidate_upload_id"]); _render_header(header_slot)
        _render_trace()
with right:
    with st.container(border=True): _render_outputs()
