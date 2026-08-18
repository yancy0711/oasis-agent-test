import numpy as np
import pandas as pd

from oasis.core.data.environment import derive_environment_burden


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
    assert result["environment_components_available"].tolist() == [2, 3, 0]
    assert np.isfinite(result["environment_burden"]).all()
