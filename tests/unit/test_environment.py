import numpy as np
import pandas as pd
import pytest

from oasis.core.data.environment import derive_environment_burden


def test_optical_zonal_aggregation_preserves_regional_zone_ids(monkeypatch):
    import geopandas as gpd
    from shapely.geometry import box
    from oasis.core.data import heat

    zones = gpd.GeoDataFrame({"GEOID": ["demo-west", "123", "06075000100"]},
                              geometry=[box(0, 0, 1, 1)] * 3, crs=4326)
    monkeypatch.setattr(heat, "_grid_definition", lambda _: (np.ones((2, 2)), None))
    monkeypatch.setattr("rasterstats.zonal_stats", lambda *args, **kwargs: [{"mean": .2, "count": 2}] * 3)
    bands = dict.fromkeys(("mean_ndvi", "mean_ndbi", "mean_albedo"))
    result = heat._zonal_environment_indices(zones, bands, "EPSG:4326")
    assert result["GEOID"].tolist() == zones["GEOID"].tolist()


def test_environment_burden_reverses_protective_indices_and_increases_with_built_up_index():
    result = derive_environment_burden(pd.DataFrame({
        "mean_ndvi": [.8, .2], "mean_ndbi": [.1, .7], "mean_albedo": [.4, .1],
    }))
    assert result.loc[0, "environment_burden"] < result.loc[1, "environment_burden"]
    assert np.all((result["environment_burden"] >= 0) & (result["environment_burden"] <= 1))


def test_environment_burden_fills_empty_rows_with_the_observed_median():
    result = derive_environment_burden(pd.DataFrame({
        "mean_ndvi": [.8, .2, np.nan], "mean_ndbi": [.1, .7, np.nan], "mean_albedo": [np.nan, .1, np.nan],
    }))
    assert result["environment_components_available"].tolist() == [2, 2, 0]
    assert np.isfinite(result["environment_burden"].iloc[:2]).all()
    assert result["environment_burden"].iloc[2] == pytest.approx(.5)


@pytest.mark.parametrize("zone_count, low_coverage", [(6, False), (7, True)])
def test_environment_coverage_is_based_on_observations_before_filling(monkeypatch, zone_count, low_coverage):
    import geopandas as gpd
    from shapely.geometry import box
    from oasis.agent.context import Context
    from oasis.contracts.domain import Provenance
    from oasis.services import workflow

    zones = gpd.GeoDataFrame({"GEOID": [str(i) for i in range(zone_count)]},
                             geometry=[box(i, 0, i + 1, 1) for i in range(zone_count)], crs=4326)
    missing = [np.nan] * (zone_count - 3)

    def reflectance(*args, **kwargs):
        return pd.DataFrame({
            "GEOID": zones["GEOID"], "mean_ndvi": [.8, .5, .2] + missing,
            "mean_ndbi": [.1, .5, .7] + missing, "mean_albedo": [.3, .2, .1] + missing,
        }), Provenance(source="Mock reflectance")

    monkeypatch.setattr(workflow, "fetch_environment_indices", reflectance)
    context = Context()
    context.put("study", {"cache_scope": "test_sparse_environment"})
    context.put("demand", {"areas": zones, "indicators": {}})
    result = workflow.prepare_built_environment(context)
    assert result.ok, result.error
    assert result.summary["available_tracts"]["mean_ndvi"] == 3
    assert result.summary["burden_observed_share"] == 3 / zone_count
    assert result.summary["burden_low_coverage"] is low_coverage
    assert result.summary["burden_imputed_zones"] == zone_count - 3
    correlation = result.summary["feature_relationships"]
    index = correlation["indicators"].index("environment_burden")
    assert correlation["paired_tract_counts"][index][index] == 3
    demand = context.get("demand")
    np.testing.assert_allclose(demand["indicators"]["environment_burden"][3:], .5)
