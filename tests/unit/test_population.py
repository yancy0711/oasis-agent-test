from oasis.core.data.population import population_raster_source, worldpop_raster_url


def test_worldpop_url_is_version_pinned():
    assert worldpop_raster_url(2022).endswith(
        "/R2025A/2022/USA/v1/100m/constrained/usa_pop_2022_CN_100m_R2025A_v1.tif"
    )


def test_population_source_can_be_disabled_or_overridden():
    assert population_raster_source(2022, {"enabled": False}) is None
    source = population_raster_source(2022, {"provider": "geotiff", "url": "local.tif"})
    assert source["url"] == "local.tif"


def test_default_population_source_is_census_block_group():
    source = population_raster_source(2022)
    assert source == {
        "provider": "census_block_group", "year": 2022,
        "resolution": "block group", "minimum_valid_population_share": 0.8,
    }
