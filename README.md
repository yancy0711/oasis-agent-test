# OASIS Agent

OASIS is a GeoAI decision-support agent for equity-aware public-health facility siting in the United States. It resolves a city or county, prepares Census, OpenStreetMap, and heat-exposure data, estimates walking access, solves location models, and produces an interactive map, a Pareto chart, and a provenance report.

Two execution modes share the same deterministic geospatial core:

- `pipeline`: runs the fixed six-stage workflow without an LLM.
- `agent`: uses an LLM to parse intent and choose the next registered action from observed results. Deterministic tools still perform geocoding, routing, validation, MCLP/LSCP solving, threshold search, and output generation.

## Architecture

```text
oasis-agent/
|-- oasis/                         # Framework-independent application package
|   |-- contracts/                 # Shared Pydantic domain contracts
|   |-- agent/                     # Context, prompts, and observation-action orchestration
|   |-- core/
|   |   |-- data/                  # Census, geocoding, Landsat, and OSM acquisition
|   |   |-- spatial/               # CRS alignment and walking-access matrices
|   |   |-- optimization/          # MCLP, LSCP, threshold search, and Pareto analysis
|   |   `-- outputs/               # Folium maps, charts, and Markdown reports
|   |-- services/                  # Registered tools, use cases, jobs, and artifacts
|   `-- settings.py                # Runtime settings and value profiles
|-- api/                           # FastAPI routes and HTTP schemas
|-- ui/                            # Streamlit presentation and API client
|-- cli/                           # Pipeline/agent commands and terminal HITL
|-- config/                        # YAML settings and value profiles
|-- data/samples/                  # Small committed input examples
|-- experiments/                   # Live agent-behavior evaluation
|-- tests/                         # Unit, integration, and Streamlit acceptance tests
|-- notebooks/                     # Research exploration, not production state
|-- Dockerfile                     # Shared backend/frontend container image
`-- docker-compose.yml             # Starts FastAPI and Streamlit together
```

Runtime flow:

```text
Streamlit UI -> FastAPI service -> OASIS services -> agent + deterministic core
                                              -> cache, outputs, and provenance
```

Web execution keeps API keys and filesystem paths in the backend. Streamlit exchanges typed tasks, opaque IDs, progress, summaries, and allow-listed artifacts through `/v1`. Uvicorn currently uses one worker because run state is in memory; caches and outputs remain on disk.

## Requirements

- Python 3.11
- GDAL-compatible geospatial libraries
- CBC solver for PuLP
- Internet access for live geocoding and data acquisition

Important Python packages include GeoPandas, Shapely, OSMnx, PyGRIS, Planetary Computer, Rasterio, PuLP, Pydantic, Folium, Matplotlib, FastAPI, Uvicorn, HTTPX, and Streamlit. Exact constraints are in `requirements.txt`.

## API keys

Copy the example environment file:

```powershell
Copy-Item .env.example .env
```

- `OPENROUTER_API_KEY`: required for agent mode.
- `ORS_API_KEY`: required for OpenRouteService walking matrices.
- `CENSUS_API_KEY`: recommended for reliable Census API access.
- `OPENROUTER_SITE_URL` and `OPENROUTER_APP_NAME`: optional OpenRouter metadata.

The deterministic pipeline can run without OpenRouter. Never commit the populated `.env` file.

## Install and test

```powershell
conda create -n oasis-agent python=3.11
conda activate oasis-agent
pip install -r requirements.txt
pytest tests -q
```

## Run from the command line

Deterministic pipeline:

```powershell
python -m cli.main --mode pipeline --place "New York City" --n-facilities 12 --threshold-minutes 10 --profile equity_first
```

Agent mode:

```powershell
python -m cli.main --mode agent --prompt "Site 8 cooling centers in Washington, DC within a 15-minute walking threshold and maximize population coverage."
```

Diagnostic examples:

```powershell
python -m cli.main --mode agent --prompt "At a strict 15-minute threshold, what is the minimum number of Phoenix cooling centers needed to cover every tract?"
python -m cli.main --mode agent --prompt "With exactly 2 Phoenix cooling centers, what is the smallest threshold that covers every tract?"
python -m cli.main --mode agent --prompt "Which Phoenix tracts are hardest to serve within 15 minutes?"
```

The profiles are `balanced`, `efficiency_first`, `equity_first`, and `cooling_first`. CLI arguments can override parsed facility count, threshold, and profile. Ambiguous places require confirmation; repeated names resolve to the same canonical cache identity.

CLI runs write to `outputs_run/<place>/`; web runs use `outputs_run/api_runs/<run_id>/<place>/`. A successful siting run produces `coverage_map.html`, `pareto.png`, and `report.md`. A heat-only review produces `heat_exposure_map.html`, `heat_analysis.png`, and `heat_report.md`.

## Run the web application

Start FastAPI in one terminal:

```powershell
uvicorn api.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Start Streamlit in another terminal:

```powershell
streamlit run ui/streamlit_app.py
```

Open `http://127.0.0.1:8501`. Health and API documentation are available at `http://127.0.0.1:8000/health` and `http://127.0.0.1:8000/docs`. Set `OASIS_API_URL` only when the services run on different hosts.


## Run with Docker Compose

Docker Compose is the recommended tester package because it supplies the same Python, GDAL, CBC, API, and UI environment on Windows, macOS, and Linux:

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Open `http://localhost:8501`; stop with `docker compose down`. `data/cache` and `outputs_run` are mounted from the host. PyInstaller is still possible, but it must be built separately on each operating system and is harder to maintain with native geospatial dependencies.

For the tool sequence and manual equivalence checks, see `PIPELINE.md`.