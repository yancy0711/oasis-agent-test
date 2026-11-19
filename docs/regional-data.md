# Regional cooling-centre siting

Confirm the study area after the assistant extracts it from the prompt. The name selects an approved data adapter. The Data tab shows the provider, vintage, demand-zone count, available population fields, and any source error. Files and field values stay out of the language-model prompt.

| Layer | Automatic sources | Local alternative |
| --- | --- | --- |
| US boundary and demographics | Existing ACS/TIGER workflow | Demand GeoJSON |
| Hong Kong boundary and demographics | C&SD 2021 district boundaries and small TPU groups, total population and age 65+ | Demand GeoJSON |
| Nanshan, Shenzhen | OpenStreetMap district boundary and 2022 WorldPop estimates, total population and age 65+ in clipped 3 km analysis zones | Demand GeoJSON |
| Other named study areas | Use local polygons and counts to define the study area | Demand GeoJSON |
| Candidate sites | OpenStreetMap for the confirmed boundary | CSV with latitude and longitude columns and a declared coordinate system |
| Heat | ERA5-HEAT UTCI with a configured CDS account. Landsat LST remains a separate layer. | Hourly ERA5-HEAT NetCDF via the backend's `OASIS_UTCI_PATH` |
| Built environment | Landsat surface reflectance, aggregated over clear pixels in demand zones. Runs independently of the thermal input. | — |
| Travel | Quick distance-based estimates, or network routing with a configured provider | Choose walking, cycling, or driving in Setup |

Whole small TPU groups are assigned to a district by their interior representative point. District totals can differ from the district-level census publication. Hong Kong uses local UTCI day boundaries, and a city district can share an ERA5-HEAT cell with surrounding districts. Household vehicle availability and comparable poverty rates are unavailable for the regional adapter and upload mode. Choose a single travel mode for these runs. The existing facility designation, independent income or other vulnerability uploads, and station-based HKHI ingestion require their own data contracts.

In Analysis, the vulnerability PCA uses varying vulnerability inputs available for this run. The pairwise correlation matrix can also include measured NDVI, NDBI and broadband albedo proxies. The optimizer adjusts the confirmed positive coefficients across the usable inputs. A UTCI field shared across the whole study area can appear on the map with the same temperature in every zone and has no spatial siting effect.

### Docker setup for UTCI

Set `CDSAPI_URL` and `CDSAPI_KEY` in the project-root `.env` after accepting the ERA5-HEAT dataset terms. For pre-downloaded NetCDF files, place them under `data/thermal/` and set `OASIS_UTCI_PATH=/app/data/thermal` in `.env`. Run `docker compose up -d --build` after switching branches or changing credentials.

## Try the workflows

1. **Automatic Hong Kong census and OSM.** Enter “在九龍城區選 3 個避暑中心，步行門檻 15 分鐘，優先服務 65 歲及以上居民。” Confirm **Kowloon City, Hong Kong**. In Data, look for 2021 census and 18 demand zones, then use **OpenStreetMap** and **Quick** in Setup. A second check is “Select 3 cooling facilities in Sham Shui Po, Hong Kong within a 15-minute walk.” The official feed gives 11 populated zones for that district.
2. **Uploaded boundary, population and sites.** Parse “Select 1 cooling centre in Kowloon City, Hong Kong within a 5-minute walk.” Drag [hong-kong-demand.geojson](examples/hong-kong-demand.geojson) onto the map, or choose it under **Data → Demand polygons and population**. Review the fields, keep EPSG:4326, and click **Confirm and use uploaded demand**. Preview six polygons under **Layers → Demand zones**. Drag [hong-kong-candidates.csv](examples/hong-kong-candidates.csv) onto the map or use **Candidate CSV**. Choose **Uploaded CSV**, **Walking**, and **Quick** in Setup. The synthetic example contains 3,490 residents, 821 people aged 65+ and six candidate points.
3. **Automatic Nanshan data.** Enter “Select 3 cooling centres in Nanshan District, Shenzhen, China within a 15-minute walk.” Confirm the district, then check **Data** for the WorldPop estimate and the boundary and demand download links. The model places each zone's demand at an interior representative point. Upload smaller demand polygons to analyze neighborhood-scale walking access.
4. **Regional download unavailable.** The Setup warning asks you to check **Data** or use **Upload demand data**. For an area without an adapter or an unavailable population service, drag a demand GeoJSON onto the map and confirm its fields. A confirmed upload supplies the boundary and population for the named area and takes precedence over automatic data. The boundary download can be used independently if its provider is available.

Combine separate boundary and population files in GIS before upload by joining on a stable zone ID. Use one Polygon or MultiPolygon feature per demand zone. Map the ID and total population, plus the optional 65+ count. Choose the coordinates declared by your provider. The demand form accepts EPSG codes such as 4326, CGCS2000 (4490), or Hong Kong 1980 Grid (2326). Set **Candidate coordinates** in **Data** before choosing or dropping a projected candidate CSV. For verified mainland GCJ-02 data, choose the explicit GCJ-02 option for each relevant file. OASIS applies an approximate inverse and uses the resulting WGS84 coordinates in both the map and solver. Check alignment visually against a known landmark. Longitude and latitude ranges alone cannot distinguish GCJ-02 from WGS84.

[Hong Kong C&SD 2021 small TPU groups](https://data.gov.hk/en-data/dataset/hk-censtatd-census_geo-2021-population-census-by-stpu) · [LCSD library locations](https://data.gov.hk/en-data/dataset/hk-lcsd-csdi-libraries) · [HAD halls and community centres](https://data.gov.hk/en-data/dataset/hk-had-json1-chcc)

[OpenStreetMap Nanshan district boundary](https://www.openstreetmap.org/relation/5664195) · [WorldPop API](https://api.worldpop.org/v3/documentation) · [AMap coordinate systems](https://lbs.amap.com/api/javascript-api-v2/guide/transform/convertfrom)
