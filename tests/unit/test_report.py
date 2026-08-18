"""Regression tests for the readable model-method report."""
from oasis.contracts.domain import SiteSolution, TaskSpec, ValueWeights
from oasis.core.outputs.report import render_report


def test_report_explains_active_and_diagnostic_only_equations():
    task = TaskSpec(place_name="Phoenix", n_facilities=8, threshold_minutes=15,
                    weights=ValueWeights())
    solution = SiteSolution(
        profile_name="balanced", selected_ids=["a"], covered_population=100,
        covered_ratio=.8, vulnerable_covered_ratio=.7,
        population_weighted_access_gini=.2, mean_access_score=.6,
        assigned_population=100, mean_travel_minutes=7,
        weights_used=ValueWeights(), solver_status="Optimal",
    )
    report = render_report(task, solution, [], [], model_context={
        "population_weighting_threshold": .8,
        "built_environment": {"status": "ready"},
        "vulnerability": {
            "active_scheme": "configured", "normalization": "percentile",
            "priority_definition": "top 25% of the active composite vulnerability score",
            "missingness": [{"indicator": "poverty_rate", "missing_count": 1,
                             "missing_percent": .01, "strategy": "median", "fill_value": .132}],
            "correlation": {"high_pairs": []},
            "vif": [{"indicator": "poverty_rate", "vif": 1.362}],
            "pca": {"explained_variance_ratio": [.6], "components_for_80_percent": 2},
            "diagnostic_correlation": {"strongest_pairs": [{
                "features": "heat_exposure and mean_ndvi", "correlation": -.7,
                "paired_tracts": 100,
            }]},
            "weight_sensitivity": {"comparisons": [
                {"rank_correlation": .9, "priority_tract_jaccard": .8}
            ]},
        },
    })
    assert r"\frac{\displaystyle\sum_{g \in i}" in report
    assert r"c_{ij} = \max" in report
    assert "Population-weighted access inequality" in report
    assert "included in $w_i$ whenever that coefficient is positive" in report
    assert "$w_i$" in report and "$c_{ij}$" in report
    assert "Vulnerability preprocessing and robustness" in report
    assert "minimum rank correlation was 0.900" in report
    assert "poverty_rate had 1 missing tract" in report
    assert "heat_exposure and mean_ndvi" in report
