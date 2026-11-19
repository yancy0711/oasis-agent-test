import numpy as np
import pandas as pd

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


def test_environment_burden_documents_partial_components_and_median_fills_empty_rows():
    result = derive_environment_burden(pd.DataFrame({
        "mean_ndvi": [.8, .2, np.nan], "mean_ndbi": [.1, .7, np.nan], "mean_albedo": [np.nan, .1, np.nan],
    }))
    assert result["environment_components_available"].tolist() == [2, 2, 0]
    assert np.isfinite(result["environment_burden"]).all()
