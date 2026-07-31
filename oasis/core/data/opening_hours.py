"""Deterministic OSM opening-hours filtering for one confirmed service window."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import geopandas as gpd
import pandas as pd
from opening_hours import OpeningHours, State


def _open_fraction(expression: str, start: datetime, end: datetime, point) -> float:
    hours = OpeningHours(expression, coords=(point.y, point.x))
    open_seconds = 0.0
    for interval_start, interval_end, state, _ in hours.intervals(start, end):
        left = max(interval_start.replace(tzinfo=None), start)
        right = min(interval_end.replace(tzinfo=None), end)
        if state == State.OPEN and right > left:
            open_seconds += (right - left).total_seconds()
    return min(open_seconds / (end - start).total_seconds(), 1.0)


def filter_candidates_by_hours(
    candidates: gpd.GeoDataFrame,
    window: Any,
) -> tuple[gpd.GeoDataFrame, dict[str, Any]]:
    """Keep fully open sites plus explicitly included unknown schedules."""
    if window is None:
        result = candidates.copy()
        result["hours_status"] = "not_evaluated"
        return result, {
            "enabled": False,
            "potential_candidates": len(result),
            "available_candidates": len(result),
        }

    start = datetime.combine(window.date, window.start)
    end = datetime.combine(window.date, window.end)
    statuses: list[str] = []
    fractions: list[float | None] = []
    expressions = (
        candidates["opening_hours"]
        if "opening_hours" in candidates
        else pd.Series(index=candidates.index, dtype=object)
    )
    for index, expression in expressions.items():
        if not isinstance(expression, str) or not expression.strip():
            statuses.append("unknown")
            fractions.append(None)
            continue
        try:
            fraction = _open_fraction(
                expression.strip(),
                start,
                end,
                candidates.loc[index].geometry,
            )
            fractions.append(fraction)
            statuses.append(
                "open" if fraction >= 1.0 - 1e-9
                else "partial" if fraction > 0
                else "closed"
            )
        except (SyntaxError, ValueError, OverflowError):
            statuses.append("invalid")
            fractions.append(None)

    result = candidates.copy()
    result["hours_status"] = statuses
    result["open_fraction"] = fractions
    unknown = result["hours_status"].isin(["unknown", "invalid"])
    keep = result["hours_status"].eq("open")
    if window.unknown_policy == "include":
        keep |= unknown
    filtered = result.loc[keep].reset_index(drop=True)
    counts = result["hours_status"].value_counts().to_dict()
    summary = {
        "enabled": True,
        "window": {
            "date": window.date.isoformat(),
            "start": window.start.isoformat(timespec="minutes"),
            "end": window.end.isoformat(timespec="minutes"),
        },
        "unknown_policy": window.unknown_policy,
        "potential_candidates": len(result),
        "available_candidates": len(filtered),
        "excluded_candidates": int((~keep).sum()),
        "known_candidates": int((~unknown).sum()),
        "unknown_candidates": int(unknown.sum()),
        "status_counts": {str(key): int(value) for key, value in counts.items()},
    }
    return filtered, summary
