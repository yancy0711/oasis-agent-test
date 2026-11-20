# Regional cooling-centre siting

Confirm the study area after the assistant extracts it from the prompt. The name selects an approved data adapter. The Data tab shows the provider, vintage, demand-zone count, available population fields, and any source error. Files and field values stay out of the language-model prompt.

## Source selection by study area

| Case | Study boundary and demand units | Population and age 65+ | Socioeconomic input |
| --- | --- | --- | --- |
| US city or county | Human-confirmed OpenStreetMap study boundary and US Census TIGER/Line tracts | ACS 5-year, default vintage 2022 | ACS B17001 poverty status and B08201 household vehicle availability |
| Kowloon City or another Hong Kong district | 2021 C&SD district boundary and small TPU groups | 2021 C&SD census counts. Kowloon City has 18 demand zones in the current adapter. | 2021 share of domestic households below HK$10,000 per month |
| Nanshan, Shenzhen | OpenStreetMap district boundary and a clipped 3 km analysis grid | WorldPop Global2 2022 estimates | CIESIN GRDI v1 |
| Gulou, Nanjing or another supported named region | Human-confirmed OpenStreetMap administrative boundary and a clipped grid with resolution chosen from the boundary extent | WorldPop Global2 2022 estimates. The verified Gulou example has 21 populated zones on a 2 km grid. | CIESIN GRDI v1 for confirmed mainland China study areas |
| Confirmed demand GeoJSON | Uploaded polygons and their combined boundary | Uploaded population and optional age 65+ fields | GRDI for mainland demand polygons. Hong Kong census shares require matching official zone IDs and boundaries. |

## Sources shared across study areas

| Layer | Automatic sources | Local alternative |
| --- | --- | --- |
| Candidate sites | OpenStreetMap for the confirmed boundary | CSV with latitude and longitude columns and a declared coordinate system |
| Heat | ERA5-HEAT UTCI with a configured CDS account. Landsat LST remains a separate layer. | Hourly ERA5-HEAT NetCDF via the backend's `OASIS_UTCI_PATH` |
| Built environment | Landsat surface reflectance, aggregated over clear pixels in demand zones. Runs independently of the thermal input. | — |
| Travel | Quick uses Haversine distance and mode assumptions. Precise uses OpenRouteService network times. | Choose walking, cycling, or driving in Setup |

Whole small TPU groups are assigned to a district by their interior representative point. District totals can differ from the district-level census publication. Hong Kong uses local UTCI day boundaries, and a city district can share an ERA5-HEAT cell with surrounding districts. Household vehicle availability and comparable poverty rates are unavailable for the regional adapter and upload mode. Choose a single travel mode for these runs. The existing facility designation, independent income or other vulnerability uploads, and station-based HKHI ingestion require their own data contracts.

On the map, **Study boundary** is the confirmed outer extent. **Demand zones** are the units that carry population and heat indicators. In the mainland workflow, OASIS overlays a projected grid, clips its cells to the administrative boundary, and aggregates WorldPop estimates to each cell. The travel-time matrix uses one geometric centroid per demand zone.

In Analysis, the vulnerability PCA uses varying vulnerability inputs available for this run. The pairwise correlation matrix can also include measured NDVI, NDBI and broadband albedo proxies. The environment burden score uses the observed median to fill zones with no usable components. Data checks, Analysis and the report warn when observed scores cover fewer than 50% of demand zones. Correlations use observed scores. The optimizer adjusts positive coefficients across usable inputs. A UTCI field shared across the whole study area can appear on the map with the same temperature in every zone and has no spatial siting effect.

## Public poverty and income data

| Data source | Geography and meaning | Application status |
| --- | --- | --- |
| [World Bank China Poverty and Inequality Platform](https://pip.worldbank.org/country-profiles/CHN) | National poverty estimates by survey year and poverty line | Context for national comparisons |
| [National Bureau of Statistics household survey](https://www.stats.gov.cn/zs/tjws/zytjzbqs/jmrj/202501/t20250121_1958392.html) | National and provincial income statistics, including urban and rural breakdowns | Context for national and provincial comparisons |
| [Hong Kong 2021 small TPU field dictionary](https://static.csdi.gov.hk/csdi-webpage/view/d5c837b46e55558b8d5fd5c18523a6ea/sds_view) | `(dhi_1 + dhi_2) / dh` measures the share of domestic households earning under HK$10,000 per month. | `low_income_household_share`. Verified for all 18 Kowloon City zones. |
| [CIESIN GRDI v1](https://doi.org/10.7927/3xxe-ap97) | Relative deprivation in index points from 0 to 100, at 30 arc-seconds (about 1 km at the equator). Higher scores indicate more deprivation. | `deprivation_index` for mainland China. Valid raster values are averaged by overlapping area with each demand zone. |

Setup names the regional indicator in the weights form. A positive coefficient affects siting when observations vary across the study area. If any demand zones lack observations and the coefficient is applied, the siting objective uses the observed median for those zones. The map and observed correlations retain missing values. Analysis and the report record the source, observed coverage, and applied coefficient.

The first mainland run downloads the public GRDI v1 GeoTIFF (184 MB) to the shared `data/cache/regional` directory. Later runs reuse it. For offline use, place the original GRDI v1 GeoTIFF in that cache or set `OASIS_GRDI_PATH` to its path. Docker mounts `./data/cache` at `/app/data/cache`.

### Docker setup for UTCI

Set `CDSAPI_URL` and `CDSAPI_KEY` in the project-root `.env` after accepting the ERA5-HEAT dataset terms. For pre-downloaded NetCDF files, place them under `data/thermal/` and set `OASIS_UTCI_PATH=/app/data/thermal` in `.env`. Run `docker compose up -d --build` after switching branches or changing credentials.

## Try the workflows

1. **Automatic Hong Kong census and OSM.** Enter “在九龍城區選 3 個避暑中心，步行門檻 15 分鐘，優先服務 65 歲及以上居民。” Confirm **Kowloon City, Hong Kong**. In Data, look for 2021 census and 18 demand zones, then use **OpenStreetMap** and **Quick** in Setup. A second check is “Select 3 cooling facilities in Sham Shui Po, Hong Kong within a 15-minute walk.” The official feed gives 11 populated zones for that district.
2. **Uploaded boundary, population and sites.** Parse “Select 1 cooling centre in Kowloon City, Hong Kong within a 5-minute walk.” Drag [hong-kong-demand.geojson](examples/hong-kong-demand.geojson) onto the map, or choose it under **Data → Demand polygons and population**. Review the fields, keep EPSG:4326, and click **Confirm and use uploaded demand**. Preview six polygons under **Layers → Demand zones**. Drag [hong-kong-candidates.csv](examples/hong-kong-candidates.csv) onto the map or use **Candidate CSV**. Choose **Uploaded CSV**, **Walking**, and **Quick** in Setup. The synthetic example contains 3,490 residents, 821 people aged 65+ and six candidate points.
3. **Automatic Nanshan data.** Enter “Select 3 cooling centres in Nanshan District, Shenzhen, China within a 15-minute walk.” Confirm the district, then check **Data** for the WorldPop estimate and the boundary and demand download links. The model places each zone's demand at its geometric centroid. Upload smaller demand polygons to analyze neighborhood-scale walking access.
4. **Regional download unavailable.** The Setup warning asks you to check **Data** or use **Upload demand data**. For an area without an adapter or an unavailable population service, drag a demand GeoJSON onto the map and confirm its fields. A confirmed upload supplies the boundary and population for the named area and takes precedence over automatic data. The boundary download can be used independently if its provider is available.

Combine separate boundary and population files in GIS before upload by joining on a stable zone ID. Use one Polygon or MultiPolygon feature per demand zone. Map the ID and total population, plus the optional 65+ count. Choose the coordinates declared by your provider. The demand form accepts EPSG codes such as 4326, CGCS2000 (4490), or Hong Kong 1980 Grid (2326). Set **Candidate coordinates** in **Data** before choosing or dropping a projected candidate CSV. For verified mainland GCJ-02 data, choose the explicit GCJ-02 option for each relevant file. OASIS applies an approximate inverse and uses the resulting WGS84 coordinates in both the map and solver. Check alignment visually against a known landmark. Longitude and latitude ranges alone cannot distinguish GCJ-02 from WGS84.

[Hong Kong C&SD 2021 small TPU groups](https://data.gov.hk/en-data/dataset/hk-censtatd-census_geo-2021-population-census-by-stpu) · [LCSD library locations](https://data.gov.hk/en-data/dataset/hk-lcsd-csdi-libraries) · [HAD halls and community centres](https://data.gov.hk/en-data/dataset/hk-had-json1-chcc)

[OpenStreetMap Nanshan district boundary](https://www.openstreetmap.org/relation/5664195) · [WorldPop API](https://api.worldpop.org/v3/documentation) · [AMap coordinate systems](https://lbs.amap.com/api/javascript-api-v2/guide/transform/convertfrom)
