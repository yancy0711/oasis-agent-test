"""Interactive preference elicitation (official HITL mechanism #1).

Triggered when TaskSpec.is_complete() is False, i.e. the LLM identified a
value-judgment gap. Deliberately lightweight -- CLI/notebook input satisfies
Phase 1; Streamlit stays optional for the final stage.

Design stance: "not sure" is a first-class answer, not an edge case. When the
human has not yet formed a value judgment, the agent's correct behavior is to
present the trade-off structure (weights stay None -> main flow runs the
Pareto sweep) -- never to decide on their behalf.
"""
from __future__ import annotations
from agent.schemas import TaskSpec, ValueWeights
from hitl.knobs import get_weights
from tools.data_acquisition.geocoding import (AmbiguousPlaceError, cache_place_candidates, canonical_place_name, select_place_candidate)

QUESTION = """
This analysis involves a value judgment that you (not the agent) must make:

How should the siting balance "cover as many people as possible" against
"prioritize historically underserved communities"?

  1) Efficiency first -- maximize total covered population
  2) Balanced
  3) Equity first -- prioritize high-poverty / elderly / heat-exposed tracts
  4) Custom -- set each weight manually
  5) Not sure -- show me both extremes and the Pareto frontier first
"""
_CHOICE = {"1": "efficiency_first", "2": "balanced", "3": "equity_first"}


def clarify(task: TaskSpec, interactive: bool = True) -> TaskSpec:
    """Fill the value gap. Non-interactive mode (tests/batch) takes the
    balanced default and says so in the notes."""
    if task.is_complete():
        return task
    if not interactive:
        task.weights = get_weights("balanced")
        task.notes += " [non-interactive: balanced default applied]"
        return task

    choice = input(QUESTION + "\nChoose (1-5): ").strip()
    if choice in _CHOICE:
        task.weights = get_weights(_CHOICE[choice])
        task.notes += f" [user chose: {_CHOICE[choice]}]"
    elif choice == "4":
        task.weights = ValueWeights(**{k: float(input(f"  {k} weight (0-1): ") or 0)
                                       for k in ValueWeights().model_dump()})
        task.notes += " [user-defined weights]"
    else:
        task.notes += " [user requested trade-off frontier before deciding]"
    return task

def _candidate_label(candidate: dict) -> str:
    """Return a concise canonical name and administrative level for HITL."""
    return f"{canonical_place_name(candidate)} ({candidate.get('addresstype', 'unknown')})"


def _candidate_number(text: str, count: int) -> int | None:
    """Parse a listed candidate number with optional ASCII/full-width parenthesis."""
    value = text.strip().rstrip(")\uff09")
    return int(value) if value.isdigit() and int(value) in range(1, count) else None


def clarify_place(task: TaskSpec, interactive: bool = True) -> TaskSpec:
    """Canonicalize one unique place or require confirmation of ambiguity."""
    query = task.place_name
    expect_level = 6 if "county" in query.lower() else None
    try:
        selected, candidates = select_place_candidate(query, expect_level)
    except AmbiguousPlaceError as exc:
        if not interactive: raise
        candidates, selected = exc.candidates[:5], exc.candidates[0]
        print(f'\nResolved "{query}" -> {_candidate_label(selected)}.')
        if len(candidates) > 1:
            print("Other matches:")
            for index, row in enumerate(candidates[1:], 1): print(f"  {index}) {_candidate_label(row)}")
        options = "1" if len(candidates) == 2 else f"1-{len(candidates) - 1}"
        answer = input(f"Use this one? [Y/n/{options}]: ").strip().lower()
        number = _candidate_number(answer, len(candidates))
        if number is not None:
            selected = candidates[number]
            task.notes += " [ambiguous place alternative selected by user]"
        elif answer in {"", "y", "yes"}:
            task.notes += " [ambiguous place default confirmed by user]"
        elif answer in {"n", "no"}:
            choice = input("Choose another number or enter a new place name: ").strip()
            number = _candidate_number(choice, len(candidates))
            if number is not None:
                selected = candidates[number]
                task.notes += " [ambiguous place alternative selected by user]"
            elif choice:
                task.place_name = choice
                task.notes += " [place query changed by user]"
                return clarify_place(task, interactive=True)
            else:
                raise ValueError("A place choice is required")
        else:
            raise ValueError("Enter Y, n, or a listed candidate number")
    cache_place_candidates(query, candidates, selected)
    task.place_name = canonical_place_name(selected)
    return task
