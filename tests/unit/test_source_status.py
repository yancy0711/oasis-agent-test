from oasis.core.data import source_status
from oasis.core.data import official_resources


def test_source_monitor_reports_updates_without_mutating_configuration():
    source_status._CACHE = None
    cfg = {"data": {"census_vintage": 2022, "source_monitor": {"ttl_hours": 0}}}

    def fake_get(url, timeout=8):
        if url.endswith("data.json"):
            return {"dataset": [{"identifier": "https://api.census.gov/data/2022/acs/acs5"},
                                {"identifier": "https://api.census.gov/data/2024/acs/acs5"}]}
        return {"status": "ok"}

    before = {"data": {"census_vintage": 2022, "source_monitor": {"ttl_hours": 0}}}
    result = source_status.inspect_data_sources(cfg, force=True, get_json=fake_get)
    assert cfg == before
    assert result["policy"] == "notify_only_no_automatic_replacement"
    assert result["updates_available"] is True
    assert {item["status"] for item in result["sources"][:2]} == {"update_available"}
    assert result["compatibility_warnings"]
    routing = next(item for item in result["sources"] if item["source"] == "openrouteservice")
    assert routing["status"] == "unverified"
    assert routing["official_url"] == "https://status.heigit.org/"


def test_source_monitor_distinguishes_unverified_from_current():
    source_status._CACHE = None

    def offline(url, timeout=8):
        raise TimeoutError

    result = source_status.inspect_data_sources(
        {"data": {"census_vintage": 2022, "source_monitor": {"ttl_hours": 0}}},
        force=True, get_json=offline,
    )
    assert result["updates_available"] is False
    assert all(item["status"] == "unverified" for item in result["sources"])


def test_official_resource_discovery_is_local_and_notify_only():
    official_resources._CACHE.clear()
    payload = {"result": {"results": [{
        "name": "phoenix-heat-relief-network",
        "title": "Phoenix Heat Relief Network",
        "notes": "City cooling-center directory",
        "organization": {"title": "City of Phoenix"},
    }]}}
    result = official_resources.discover_official_resources(
        "Phoenix, Arizona, USA", search=lambda query: payload, ttl_hours=0,
    )
    assert result["status"] == "found" and result["place_type"] == "city"
    assert result["resources"][0]["url"].endswith("phoenix-heat-relief-network")
    assert "verify" in result["resources"][0]["verification"].lower()


def test_official_resource_discovery_skips_state_level_queries():
    called = False
    def search(_query):
        nonlocal called
        called = True
        return {}
    result = official_resources.discover_official_resources("Arizona, USA", search=search)
    assert result["status"] == "not_applicable"
    assert called is False
