"""Compare two independent, solver-verified edits without changing coverage policy."""
import math

from oasis.contracts.domain import RecoveryProposal, TaskSpec


def coverage_recovery(task: TaskSpec, facilities: dict, threshold: dict, diagnosis: str, accuracy: str) -> RecoveryProposal:
    options = []
    for field, evidence in (("n_facilities", facilities), ("threshold_minutes", threshold)):
        count_edit = field == "n_facilities"
        value = (evidence.get("minimum_facilities") or evidence.get("feasible_facility_count")) if count_edit else evidence.get("minimum_threshold_minutes")
        current = getattr(task, field)
        budget_label = "facility" if task.n_facilities == 1 else "facilities"
        fixed = f"keep {task.threshold_minutes:g} minutes" if count_edit else f"keep {task.n_facilities} {budget_label}"
        option = {"parameter": field, "status": "unknown", "description": f"{fixed.capitalize()}: no verified alternative yet.",
                  "changes": {}, "relative_increase": None, "accuracy": accuracy}
        same_inputs = (
            evidence.get("coverage_target") == task.coverage_target
            and evidence.get("coverage_group") == task.coverage_group
            and (evidence.get("threshold_minutes") == task.threshold_minutes if count_edit else evidence.get("n_facilities") == task.n_facilities)
        )
        if evidence.get("verified_for_siting") and evidence.get("feasible") and same_inputs and value is not None and math.isfinite(value) and value > current:
            proposed = int(value) if count_edit else round(float(value), 1)
            changes = {field: proposed}
            try:
                TaskSpec.model_validate({**task.model_dump(), **changes})
            except ValueError:
                option.update(status="unsupported", description=f"{fixed.capitalize()}: the computed value {proposed:g} is outside the supported task range; no edit will be applied.")
            else:
                proven = evidence.get("minimum_proven", False) if count_edit else evidence.get("search_complete", False)
                amount = f"{proposed} facilities" if count_edit else f"{proposed:.1f} minutes"
                option.update(status="verified", proposed_value=proposed, changes=changes,
                              minimum_proven=proven, relative_increase=(proposed-current)/current,
                              description=f"{fixed.capitalize()}: use {amount}. " + ("Minimum proven" if proven else "Feasible; minimum not proven") + (" on the 0.1-minute grid." if not count_edit and proven else "."))
        elif evidence.get("solver_status") == "Infeasible" and count_edit and same_inputs:
            option.update(status="infeasible", description=f"{fixed.capitalize()}: increasing the count alone cannot meet the target with the current candidates and constraints.")
        elif evidence.get("search", {}).get("status") == "routing_scope_limited":
            option["description"] = f"{fixed.capitalize()}: the network matrix does not support a verified threshold alternative. Review the routing scope or candidate set."
        elif evidence.get("feasible"):
            option["description"] = f"{fixed.capitalize()}: the diagnostic does not establish a valid single-input increase for this task; no edit will be applied."
        options.append(option)
    verified = [option for option in options if option["status"] == "verified"]
    recommended = min(verified, key=lambda option: option["relative_increase"]) if verified else None
    question = f"Coverage target stays {task.coverage_target * 100:g}%; group, mode, and candidate source are unchanged."
    if recommended:
        label = "facility count" if recommended["parameter"] == "n_facilities" else "travel threshold"
        question += f" Recommended edit: {label} (+{recommended['relative_increase']:.1%}), the smallest relative increase among verified options."
    else:
        question += " No verified, supported single-input repair is available; review the diagnostics before retrying."
    return RecoveryProposal(diagnosis=diagnosis, question=question, alternatives=options,
                            suggested_changes=recommended["changes"] if recommended else {})
