"""Streamlit HITL front end for parsing, place selection, and pipeline review."""
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from agent.orchestrator import LLMClient, MissingPlaceError, Orchestrator
from agent.schemas import TaskSpec
from hitl.knobs import get_weights, load_profiles
from main import load_cfg
from tools.data_acquisition.geocoding import (AmbiguousPlaceError, cache_place_candidates,
                                               canonical_place_name, select_place_candidate)
from tools.workflow import run_tool_chain


def _candidate_label(candidate: dict) -> str:
    """Return a readable candidate label with its stable OSM identity."""
    identity = f"{candidate.get('osm_type', '?')}/{candidate.get('osm_id', '?')}"
    return f"{canonical_place_name(candidate)} ({candidate.get('addresstype', 'unknown')}; {identity})"


def _matching_profile(task_data: dict, profiles: dict) -> str | None:
    """Return the registered profile matching parsed weights exactly."""
    weights = task_data.get("weights")
    return next((name for name, profile in profiles.items() if weights == profile["weights"]), None)


def _show_outputs(outputs: dict) -> None:
    """Embed the generated map, Pareto chart, and report in three tabs."""
    map_tab, chart_tab, report_tab = st.tabs(["Interactive map", "Pareto frontier", "Report"])
    with map_tab: components.html(Path(outputs["map"]).read_text(encoding="utf-8"), height=720)
    with chart_tab: st.image(outputs["pareto"], use_container_width=True)
    with report_tab: st.markdown(Path(outputs["report"]).read_text(encoding="utf-8"))


st.set_page_config(page_title="OASIS HITL", layout="wide")
st.title("OASIS human-in-the-loop analysis")
st.caption("Parse a request, confirm geography and values, then run the deterministic six-tool pipeline.")
cfg, profiles = load_cfg(), load_profiles()
prompt = st.text_area("Natural-language request", height=100,
                      placeholder="Site 8 cooling centers in NYC within 15 minutes while maximizing population coverage.")
if st.button("Parse request", type="primary", disabled=not prompt.strip()):
    agent_cfg = {**cfg, "value_profiles": profiles}
    try: data = Orchestrator(LLMClient(cfg["llm"]), agent_cfg).parse_task(prompt).model_dump()
    except MissingPlaceError as exc: data = exc.task_data; st.warning(str(exc))
    st.session_state.task_data = data
    st.session_state.place_input = data.get("place_name", "")
    st.session_state.n_input = int(data.get("n_facilities", cfg["optimization"]["n_facilities"]))
    st.session_state.threshold_input = float(data.get("threshold_minutes", cfg["accessibility"]["threshold_minutes"]))
    st.session_state.profile_input = _matching_profile(data, profiles)
    for key in ("place_candidates", "resolved_place", "resolved_query", "outputs"): st.session_state.pop(key, None)

if "task_data" in st.session_state:
    st.subheader("Confirm task")
    left, middle, right = st.columns(3)
    with left: place = st.text_input("US city or county", key="place_input")
    with middle: n = st.number_input("Facilities", min_value=1, max_value=100, step=1, key="n_input")
    with right: threshold = st.number_input("Walking threshold (minutes)", min_value=.1, step=1., key="threshold_input")
    profile = st.selectbox("Value profile", list(profiles), index=None, key="profile_input",
                           placeholder="Choose a value profile",
                           format_func=lambda name: f"{name} - {profiles[name]['label']}")
    if st.button("Resolve place", disabled=not place.strip()):
        try:
            selected, candidates = select_place_candidate(place)
            st.session_state.resolved_place = canonical_place_name(selected)
            st.session_state.resolved_query = place
            st.session_state.pop("place_candidates", None)
        except AmbiguousPlaceError as exc:
            st.session_state.place_candidates = exc.candidates[:5]
            st.session_state.pop("resolved_place", None)
    if "place_candidates" in st.session_state:
        candidates = st.session_state.place_candidates
        choice = st.selectbox("Choose the intended boundary", range(len(candidates)),
                              format_func=lambda index: _candidate_label(candidates[index]))
        if st.button("Confirm candidate"):
            selected = candidates[choice]; cache_place_candidates(place, candidates, selected)
            st.session_state.resolved_place = canonical_place_name(selected)
            st.session_state.resolved_query = place
            st.rerun()
    resolved = st.session_state.get("resolved_place") if st.session_state.get("resolved_query") == place else None
    if resolved: st.success(f"Confirmed study area: {resolved}")
    ready = bool(resolved and profile)
    if st.button("Run confirmed pipeline", type="primary", disabled=not ready):
        data = st.session_state.task_data
        task = TaskSpec(place_name=resolved, intervention=data.get("intervention", "cooling_center"),
                        n_facilities=int(n), threshold_minutes=float(threshold), weights=get_weights(profile),
                        candidate_amenities=data.get("candidate_amenities", ["library", "community_centre", "school"]),
                        notes=f"{data.get('notes', '')} [Streamlit HITL profile: {profile}]".strip())
        with st.status("Running six-tool pipeline", expanded=True) as status:
            result = run_tool_chain(cfg, task)
            status.update(label="Pipeline complete" if result["ok"] else f"Stopped at {result['failed_tool']}",
                          state="complete" if result["ok"] else "error")
        if result["ok"]: st.session_state.outputs = result["outputs"]
        else: st.error(result["results"][-1].error.message)
if "outputs" in st.session_state: _show_outputs(st.session_state.outputs)