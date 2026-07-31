import numpy as np

from oasis.core.optimization.mobility import compose_demand


def test_walk_drive_equity_expands_population_without_double_counting():
    demand = {
        "population": np.array([100.0, 200.0]),
        "no_vehicle_ratio": np.array([0.25, 0.50]),
        "vulnerable_mask": np.array([True, False]),
        "indicators": {"poverty_rate": np.array([0.4, 0.2])},
    }
    matrices = {
        "walking": np.array([[1.0], [2.0]]),
        "driving": np.array([[3.0], [4.0]]),
    }

    result = compose_demand(demand, matrices, "walk_drive_equity")

    assert result["matrix"].shape == (4, 1)
    assert result["population"].sum() == 300
    assert np.array_equal(result["base_index"], [0, 1, 0, 1])
    assert result["mode_population_share"] == {
        "walking": 125 / 300,
        "driving": 175 / 300,
    }


def test_single_mode_preserves_original_rows():
    demand = {
        "population": np.array([100.0]),
        "vulnerable_mask": np.array([True]),
        "indicators": {"poverty_rate": np.array([0.4])},
    }
    matrix = np.array([[7.0, 8.0]])

    result = compose_demand(demand, {"cycling": matrix}, "cycling")

    assert np.array_equal(result["matrix"], matrix)
    assert result["mode_population_share"] == {"cycling": 1.0}
