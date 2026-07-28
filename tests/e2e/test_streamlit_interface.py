"""Offline tests for Streamlit prompts and confirmed-task readiness."""
from pathlib import Path
import subprocess
import sys

from streamlit.testing.v1 import AppTest

from oasis.settings import get_weights


def test_constraint_task_can_run_after_place_confirmation():
    """A hard coverage target must not disable a fully confirmed balanced task."""
    app = AppTest.from_file("ui/streamlit_app.py")
    place = "Phoenix, Arizona, USA"
    app.session_state["task_data"] = {
        "place_name": place, "analysis_goal": "facility_siting", "candidate_source": "osm",
        "n_facilities": 2, "threshold_minutes": 15, "coverage_target": 1.0, "weights": None,
        "candidate_amenities": ["library", "community_centre", "school"],
        "preference_question": "Which concern should be prioritized?", "notes": "",
    }
    app.session_state["place_input"] = place
    app.session_state["resolved_place"] = place; app.session_state["resolved_query"] = place
    app.session_state["n_input"] = 2; app.session_state["threshold_input"] = 15.0
    app.session_state["profile_input"] = "balanced"; app.session_state["profile_defaulted"] = True
    app.session_state["candidate_source_input"] = "osm"
    for field, value in get_weights("balanced").model_dump().items():
        app.session_state[f"weight_{field}"] = value
    app.run(timeout=30)
    buttons = {button.label: button.disabled for button in app.button}
    assert not app.exception and buttons["Run confirmed agent"] is False
    assert app.warning and "100% of tracts" in app.warning[0].value


def test_start_page_exposes_copyable_agent_examples():
    """The initial page must show the four behavioral test prompts."""
    app = AppTest.from_file("ui/streamlit_app.py").run(timeout=30)
    code = "\n".join(block.value for block in app.code)
    assert "candidate sites in a CSV" in code and "highest heat exposure" in code
    assert "every tract must be reachable" in code and "15-minute walking threshold" in code

def test_recovery_button_updates_widgets_before_the_next_render():
    """A confirmed recovery must update keyed controls without a session-state error."""
    app = AppTest.from_file("ui/streamlit_app.py")
    app.session_state["task_data"] = {"place_name": "Phoenix, Arizona, USA", "analysis_goal": "facility_siting",
                                      "candidate_source": "osm", "n_facilities": 2, "threshold_minutes": 15,
                                      "coverage_target": 1.0, "weights": None}
    app.session_state["n_input"] = 2; app.session_state["threshold_input"] = 15.0
    app.session_state["profile_input"] = "balanced"; app.session_state["candidate_source_input"] = "osm"
    app.session_state["recovery"] = {
        "diagnosis": "The target needs a larger budget.", "question": "Use three facilities?",
        "suggested_changes": {"n_facilities": 3},
    }
    app.session_state["diagnostics"] = {
        "minimum_facilities": {"feasible": True, "minimum_facilities": 3, "threshold_minutes": 15},
        "minimum_threshold": {"feasible": True, "minimum_threshold_minutes": 21.0, "n_facilities": 2},
    }
    app.run(timeout=30)
    assert any("Analysis stopped" in item.value for item in app.error)
    metrics = {item.label: item.value for item in app.metric}
    assert metrics["Minimum facilities"] == "3" and metrics["Minimum threshold"] == "21.0 min"
    next(button for button in app.button if button.label == "Apply suggested changes").click().run(timeout=60)
    assert not app.exception and app.session_state["n_input"] == 3
    assert any("Feasibility diagnostic complete" in item.value for item in app.success)

def test_expanded_example_button_fills_the_request_field():
    """Example prompts must be usable without relying on browser clipboard permission."""
    app = AppTest.from_file("ui/streamlit_app.py").run(timeout=30)
    next(button for button in app.button if button.label == "Use this prompt").click().run(timeout=30)
    assert app.text_input[0].value == "Site 8 cooling centers in Phoenix, Arizona within a 15-minute walking threshold."
    assert not app.exception

def test_entry_bootstraps_project_root_from_ui_directory():
    """The documented Streamlit command must import OASIS from the UI script path."""
    root = Path(__file__).resolve().parents[2]
    code = ("from streamlit.testing.v1 import AppTest; "
            "app=AppTest.from_file('streamlit_app.py').run(timeout=30); "
            "assert not app.exception, app.exception")
    result = subprocess.run([sys.executable, "-c", code], cwd=root / "ui",
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr