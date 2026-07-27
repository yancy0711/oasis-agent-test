# OASIS Agent

OASIS is a GeoAI workflow for equity-aware public health facility siting. It resolves a US city or county, acquires census, OpenStreetMap, and heat-exposure data, estimates walking accessibility, optimizes facility locations, and produces decision-support outputs.

The project supports two execution modes:

- `pipeline`: runs the deterministic six-tool workflow without an LLM.
- `agent`: uses an OpenRouter model to parse a natural-language request, confirm ambiguous inputs through human-in-the-loop prompts, plan the tools, and execute the same workflow.

Each successful run writes three files under `outputs_run/<place>/`:

- `coverage_map.html`: interactive coverage, selected facilities, census tracts, and heat exposure.
- `pareto.png`: efficiency and equity trade-off chart.
- `report.md`: selected sites, coverage statistics, parameters, and provenance.

## Requirements

- Python 3.11
- GDAL-compatible geospatial libraries
- CBC solver for PuLP
- Internet access for live geocoding and data acquisition

Major Python libraries include GeoPandas, Shapely, PyProj, OSMnx, pygris, Planetary Computer, OpenRouteService-compatible requests, PuLP, Pydantic, Folium, Matplotlib, and Streamlit. Full versions are listed in `requirements.txt`.

## API keys

Copy `.env.example` to `.env` and fill in the required values:

```powershell
Copy-Item .env.example .env
```

- `OPENROUTER_API_KEY`: required for `agent` mode. Create one at [OpenRouter](https://openrouter.ai/).
- `ORS_API_KEY`: required when the default OpenRouteService walking matrix is used. Create one at [openrouteservice](https://openrouteservice.org/sign-up/).
- `CENSUS_API_KEY`: recommended for more reliable US Census API access. Request one from the [US Census Bureau](https://api.census.gov/data/key_signup.html).
- `OPENROUTER_SITE_URL`: optional application URL sent to OpenRouter.
- `OPENROUTER_APP_NAME`: optional application name sent to OpenRouter.

Never commit the populated `.env` file.

## Local installation

```powershell
conda create -n oasis-agent python=3.11
conda activate oasis-agent
pip install -r requirements.txt
pytest tests -q
```

The production workflow does not depend on Notebook global variables. `notebooks/explore.ipynb` is retained for exploration only.

## Run the deterministic pipeline

```powershell
python main.py --mode pipeline --place "New York City" --n-facilities 12 --threshold-minutes 10 --profile equity_first
```

Available value profiles are `balanced`, `efficiency_first`, `equity_first`, and `cooling_first`. Ambiguous place names are presented as candidates for user confirmation. Large study areas or requests exceeding the routing policy limits use the documented Haversine fallback.

## Run the agent

```powershell
python main.py --mode agent --prompt "Site 8 cooling centers in Washington, DC within a 15-minute walking threshold and maximize population coverage."
```

The agent uses the LLM only for task parsing and tool planning. Geocoding validation, data processing, routing, optimization, caching, and output generation remain deterministic tools.

CLI values can override parameters parsed from the prompt:

```powershell
python main.py --mode agent --prompt "Plan cooling centers in NYC" --n-facilities 10 --threshold-minutes 12 --profile balanced
```

## Streamlit interface

```powershell
streamlit run streamlit_app.py
```

Open the displayed local URL to enter a request, review human-in-the-loop decisions, run the workflow, and inspect the generated outputs.

## Docker

```powershell
docker build -t oasis-agent .
docker run --rm -it --env-file .env oasis-agent python main.py --mode agent --prompt "Site 8 cooling centers in Washington, DC."
```

The Docker image installs the required GDAL and CBC system packages. Mount `outputs_run` as a volume if generated files must persist after the container exits.

For the formal tool sequence and manual equivalence checks, see `PIPELINE.md`.
