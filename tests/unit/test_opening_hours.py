from datetime import date, time
from types import SimpleNamespace

import geopandas as gpd
from shapely.geometry import Point

from oasis.contracts.domain import TaskSpec
from oasis.core.data.opening_hours import filter_candidates_by_hours
from oasis.services.workflow import _filter_candidate_access


def _window(unknown_policy="include"):
    return SimpleNamespace(
        date=date(2026, 7, 29),
        start=time(14, 0),
        end=time(18, 0),
        unknown_policy=unknown_policy,
    )


def test_hours_filter_keeps_open_and_included_unknown_candidates():
    candidates = gpd.GeoDataFrame(
        {
            "cand_id": ["open", "partial", "closed", "missing"],
            "opening_hours": [
                "Mo-Fr 09:00-19:00",
                "Mo-Fr 09:00-17:00",
                "Sa-Su 09:00-17:00",
                None,
            ],
        },
        geometry=[Point(-97.7, 30.2)] * 4,
        crs="EPSG:4326",
    )

    filtered, summary = filter_candidates_by_hours(candidates, _window())

    assert filtered["cand_id"].tolist() == ["open", "missing"]
    assert summary["status_counts"] == {
        "open": 1,
        "partial": 1,
        "closed": 1,
        "unknown": 1,
    }
    assert summary["excluded_candidates"] == 2


def test_hours_filter_can_exclude_unknown_candidates():
    candidates = gpd.GeoDataFrame(
        {"cand_id": ["missing"], "opening_hours": [None]},
        geometry=[Point(-97.7, 30.2)],
        crs="EPSG:4326",
    )

    filtered, summary = filter_candidates_by_hours(
        candidates,
        _window("exclude"),
    )

    assert filtered.empty
    assert summary["unknown_candidates"] == 1

def test_default_candidates_exclude_schools():
    task = TaskSpec(place_name="Austin")

    assert task.candidate_amenities == [
        "library", "community_centre", "sports_centre", "arts_centre"
    ]


def test_access_filter_excludes_explicitly_restricted_sites():
    candidates = gpd.GeoDataFrame(
        {
            "cand_id": ["public", "unknown", "private", "members"],
            "access": ["yes", None, "private", "members"],
        },
        geometry=[Point(-97.7, 30.2)] * 4,
        crs="EPSG:4326",
    )

    filtered, summary = _filter_candidate_access(candidates)

    assert filtered["cand_id"].tolist() == ["public", "unknown"]
    assert filtered["public_access_status"].tolist() == ["osm_public", "unknown"]
    assert filtered["cooling_service_status"].tolist() == ["unverified", "unverified"]
    assert summary["restricted_candidates"] == 2
