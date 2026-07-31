"""Compact trace presentation regression tests."""
from streamlit.testing.v1 import AppTest


def test_auto_is_the_default_execution_mode():
    """A fresh UI session should prefer quick feedback followed by ORS refinement."""
    app = AppTest.from_file("ui/streamlit_app.py")
    app.run(timeout=30)

    execution_mode = next(item for item in app.selectbox if item.label == "Execution mode")
    assert execution_mode.value == "auto"


def test_run_trace_is_a_single_collapsed_compact_section():
    """Trace history must not lengthen the main page unless explicitly opened."""
    app = AppTest.from_file("ui/streamlit_app.py")
    app.session_state["agent_history"] = [
        {
            "stage": "tool_result",
            "tool": "prepare_demand",
            "message": "failed",
            "data": {},
        }
    ]
    place = "Phoenix, Arizona, USA"
    app.session_state["task_data"] = {"place_name": place, "analysis_goal": "heat_review"}
    app.session_state["place_input"] = place
    app.session_state["resolved_place"] = place
    app.session_state["resolved_query"] = place
    app.run(timeout=30)

    assert any(item.label == "Run trace (1 events)" for item in app.expander)
    assert not any(item.value == "Run trace" for item in app.subheader)
