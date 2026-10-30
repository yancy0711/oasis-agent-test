# OASIS Agent

OASIS is an auditable autonomous GIS and LLM-GIS decision-support agent for evaluating access to existing cooling-facility candidates in the United States.

## Intended users and purpose

- Policy analysts and planners: compare candidate-facility portfolios under budget, travel-time, capacity, and equity constraints.
- Public-health teams: inspect population-weighted heat exposure, vulnerable-population coverage, and hard-to-reach areas.
- Academic researchers: run reproducible efficiency-Gini, mobility, vulnerability, and threshold sensitivity analyses with provenance.
- Residents and visitors: find nearby candidate cooling places and routes from a confirmed US starting point, without a study-area run or facility budget.

OASIS is not a real-time navigation or emergency service. Researcher map routes use selected facilities from a confirmed study-area run. Resident search checks nearby OSM candidates independently. Neither workflow verifies live opening status, provides emergency advice, searches vacant land, or makes zoning decisions.

## Main capabilities

- Natural-language routing: full analysis, quick diagnostic, clarification, or out of scope.
- US city/county resolution with explicit confirmation.
- ACS/TIGER demand data, population-weighted Landsat LST, environmental diagnostics, and OSM/uploaded candidate facilities.
- Walking, cycling, and driving accessibility using ORS, with bounded Haversine preview/fallback.
- Gradual coverage, explicit assignment, optional capacity, population-weighted Gini, and interactive Pareto solutions.
- Vulnerability preprocessing: missingness, direction, normalization, correlation, VIF, PCA, alternative weights, and robustness scans.
- MapLibre maps, point/address route planning, route heat audit, and optional Mapillary imagery.
- Reports, charts, provenance, and notify-only source-version checks.
- OpenAI-compatible BYOK for Gemini, OpenAI, OpenRouter, approved custom endpoints, and local Ollama/vLLM.

Candidate facilities are possible existing service locations, not confirmed cooling centers. Unknown opening hours, fees, access, capacity, and official status remain explicit.

An optional, default-off **hypothetical-site extension** explores new locations for fixed-time coverage planning. Its separate map and counts are approximate post-paper results, not verified premises or network coverage.

## Use cases

**Researcher / planner.** Analyze cooling-facility access in a US city or county, diagnose underserved areas, compare scenarios, and explore efficiency-equity Pareto trade-offs. Results include interactive maps, coverage and fairness metrics, reports, and data provenance.

```text
Select 10 candidate cooling facilities in Riverside, California within a 15-minute walk.
Which Riverside tracts are hardest to serve? Compare efficiency and access inequality.
How many facilities are needed to cover all modeled demand within a 10-minute walk in Riverside? Also find the minimum threshold with a budget of 10 facilities.
```

**Individual resident.** Find nearby candidate cooling places from an address, a map point, or your current location. Filter by place category and walking, cycling, or driving time, then select a destination for directions. No facility budget or analysis run is required; listed places are not guaranteed to be officially designated or currently open cooling centers.

```text
I'm at Riverside Convention Center, California. Find shopping malls within a 15-minute walk.
```

## Code layout

```text
oasis-agent/
|-- api/                 # FastAPI routes and HTTP schemas
|-- cli/                 # Command-line entry and terminal confirmation
|-- config/              # Runtime settings and value profiles
|-- oasis/
|   |-- agent/           # Scope routing and bounded LLM orchestration
|   |-- contracts/       # Shared typed domain models
|   |-- core/            # Data, spatial, optimization, vulnerability, outputs
|   `-- services/        # Tool registry, workflows, run service, SQLite state
|-- frontend/
|   `-- src/
|       |-- api/         # API client and generated OpenAPI types
|       |-- components/  # Shared UI controls
|       |-- features/    # Task, resident search, run, result, and comparison interfaces
|       |-- store/       # Browser-session state
|       `-- styles/      # Global responsive styling
|-- tests/               # Unit and integration tests
|-- experiments/         # Research evaluation scripts
|-- notebooks/           # Minimal API-driven agent quickstart
```

See [PIPELINE.md](PIPELINE.md) for the computational workflow and [DEPLOYMENT.md](DEPLOYMENT.md) for Docker setup and Mac migration.

The LLM interprets intent into typed contracts; deterministic tools acquire data, build matrices, solve the confirmed model, and record provenance. Research analysis uses a confirmed city/county and produces a facility portfolio. Resident search uses a confirmed point and finds nearby existing places without invoking the portfolio solver. UI mode overrides remain available. Efficiency-Gini exploration is an approximate trade-off frontier, not a certified globally optimal Gini frontier.

## Requirements

- [Python 3.11](https://www.python.org/downloads/)
- [Node.js 22+](https://nodejs.org/en/download)
- GDAL-compatible geospatial libraries
- [CBC solver](https://github.com/coin-or/Cbc) for PuLP
- Internet access for live data and routing

If a system Python installation cannot resolve GDAL, use
[Miniconda](https://www.anaconda.com/docs/getting-started/miniconda/install) and the environment steps below.

## Data and API access

| Service | Used for | Access |
|---|---|---|
| [US Census ACS 5-year API](https://www.census.gov/data/developers/data-sets/acs-5year.html) | Population and vulnerability indicators | [Request a Census API key](https://api.census.gov/data/key_signup.html) |
| [US Census TIGER/Line](https://www.census.gov/geographies/mapping-files/time-series/geo/tiger-line-file.html) | Tract, block-group, city, and county geometry | Public download; no key |
| [Microsoft Planetary Computer Landsat](https://planetarycomputer.microsoft.com/dataset/group/landsat) | LST, NDVI, NDBI, and albedo diagnostics | Public catalog; no key |
| [WorldPop data](https://data.worldpop.org/GIS/Population/Global_2015_2030/) and [STAC API](https://api.stac.worldpop.org/api.html) | Optional 100 m population grid | Public download; no key for normal access |
| [OpenStreetMap](https://www.openstreetmap.org/copyright) and [Nominatim](https://operations.osmfoundation.org/policies/nominatim/) | Candidate facilities and geocoding | Public services; optional [hosted Overpass](https://overpass.ch/docs) requires its own key |
| [openrouteservice](https://api.heigit.org/openrouteservice/) | Network matrices and point routes | [Create an account and API key](https://account.heigit.org/) |
| [Mapillary](https://www.mapillary.com/dashboard/developers) | Optional nearby street-level imagery | Register an app and copy its client token |
| [OpenRouter](https://openrouter.ai/settings/keys), [Google Gemini](https://aistudio.google.com/apikey), or [OpenAI](https://platform.openai.com/api-keys) | Language-model parsing and orchestration | OpenRouter with openai/gpt-4o is the default; create a provider key or configure local Ollama |

For the CARTO basemap, [request a Basemaps API key](https://www.carto.com/basemaps/apikey/).

For a local language model, install [Ollama](https://ollama.com/download); OASIS does not download models automatically.

Copy the environment template:

```powershell
Copy-Item .env.example .env
```

Relevant keys:

```dotenv
GEMINI_API_KEY=
OPENAI_API_KEY=
OPENROUTER_API_KEY=
ORS_API_KEY=
CENSUS_API_KEY=
MAPILLARY_ACCESS_TOKEN=
CARTO_BASEMAP_API_KEY=
OVERPASS_CH_API_KEY=
```

Put the server-default OpenRouter key in `OPENROUTER_API_KEY` inside the project-root `.env`. A visitor may instead enter a provider key in **Preliminary setup**; that key stays only in the browser-scoped backend session. Direct Gemini remains available through `GEMINI_API_KEY`, but access is evaluated from the backend network location and is limited to Google's [supported regions](https://ai.google.dev/gemini-api/docs/available-regions). If unavailable, select OpenRouter, OpenAI, or a local model instead; OASIS never switches providers silently. Gemini free-tier availability and quotas also depend on the selected model, account, and region. Only configure the services you use, and never commit the populated `.env`.

Keep all server-default keys in the project-root `.env`; `.env.example` is the
empty template, not a second configuration file. Do not put service keys in
frontend files. The backend loads `.env` locally, and Docker Compose passes the
same file to the backend container.

`CARTO_BASEMAP_API_KEY` is the one browser-visible key: `/api/v1/config` supplies
it to the result, comparison, and resident maps, which append it to CARTO tile URLs.
LLM, Census, ORS, and Mapillary credentials stay on the backend. Without a CARTO
key, maps retain the study-area layers on a neutral background.

After changing `.env`, restart the local backend and reload the browser. With
Docker Compose, run `docker compose up -d --force-recreate backend`, then `docker compose restart frontend` and reload;
a container restart alone does not reload `env_file`. Changing keys does not
require rebuilding the frontend. Visitor-provided LLM keys remain temporary
session overrides rather than being written to `.env`.

## Install

```powershell
conda create -n oasis-agent python=3.11
conda activate oasis-agent
pip install -r requirements.txt
npm --prefix frontend install
```

## Run the web application

Run both terminals from the repository root.

Terminal 1 (backend):

```powershell
conda activate oasis-agent
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Terminal 2 (frontend):

```powershell
npm --prefix frontend run dev
```

Open http://127.0.0.1:5173.

For access from a phone or another device on the same local network, listen on
all interfaces in both terminals:

```powershell
# Terminal 1
conda activate oasis-agent
python -m uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers 1

# Terminal 2
npm --prefix frontend run dev -- --host 0.0.0.0
```

Open `http://<computer-lan-ip>:5173` on the other device. Both devices must be
on the same network, and the operating-system firewall must allow the private
network connection to port 5173. Do not expose this development server directly
to the public internet.

## Command line

```powershell
python -m cli.main --mode pipeline --place "New York City" --n-facilities 12 --threshold-minutes 10 --profile equity_first
python -m cli.main --mode agent --prompt "Select 10 candidate cooling facilities in Phoenix within a 15-minute walk."
```

The default terminal view shows confirmation choices, a short success or failure summary, actionable warnings,
and output locations. Add `--verbose` only when you need progress, network, or debugging logs.

The CLI writes analysis artifacts but does not reproduce the browser's interactive maps, selectable Pareto points,
comparison map, or point-route planner. Start the web application for those features.

## Outputs

Web runs are stored under `outputs_run/api_runs/<run_id>/`. Outputs include MapLibre GeoJSON, charts, Markdown reports, provenance, progress, and run records.

## Deployment

```powershell
docker compose up --build
```

Open http://127.0.0.1:5173. The frontend proxies `/api` requests to the backend
container on port 8000. Runtime cache, output, and uploaded-candidate directories are mounted from
`data/cache/`, `outputs_run/`, and `tmp/uploads/`. Images exclude `.env`; Compose supplies it at runtime. System CBC is preferred so Intel and Apple Silicon builds use the installed native binary.

See [DEPLOYMENT.md](DEPLOYMENT.md) for Mac setup, configuration updates, migration limits, and pre-demo checks. A standalone `docker build` does not start both services or configure their volumes.

## Verify

```powershell
python -m pytest tests -q
npm --prefix frontend test
npm --prefix frontend run build
npm --prefix frontend run lint
npm --prefix frontend run test:e2e
```

Browser tests use mocked APIs; live data and solver results require separate validation.
