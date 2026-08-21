# OASIS Agent

OASIS is an auditable autonomous GIS and LLM-GIS decision-support agent for evaluating access to existing cooling-facility candidates in the United States.

## Intended users and purpose

- Policy analysts and planners: compare candidate-facility portfolios under budget, travel-time, capacity, and equity constraints.
- Public-health teams: inspect population-weighted heat exposure, vulnerable-population coverage, and hard-to-reach areas.
- Academic researchers: run reproducible efficiency-Gini, mobility, vulnerability, and threshold sensitivity analyses with provenance.
- Residents and visitors: plan a route from a confirmed point to a selected candidate facility after a study-area run.

OASIS is not a real-time navigation or emergency service. Its route planner uses the candidate facilities computed for a confirmed study area; it does not verify live opening status, provide emergency advice, search vacant land, or make zoning decisions.

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

## Supported question types and boundaries

Supported requests fall into four user-facing workflows:

1. **Full analysis:** select existing candidate facilities and evaluate efficiency, coverage, vulnerability, Gini, capacity, environment, and Pareto trade-offs.
2. **Quick diagnostic:** rank or inspect tracts using available heat exposure, configured vulnerability, candidate accessibility, or a transparent vulnerability-access gap score.
3. **Route planning:** confirm a point or browser location, prepare candidates in its city or county, and request an ORS route to a nearby selected facility.
4. **Comparison:** compare registered value profiles, mobility assumptions, saved runs, selected facilities, and efficiency-equity outcomes.

`Needs clarification` and `out of scope` are routing outcomes rather than additional workflows. A request needs clarification when its place, origin, comparison profiles, or decision parameters cannot be confirmed safely.

The built-in tract evidence is limited to Census population and configured vulnerability indicators, Landsat-derived heat and environmental diagnostics, and computed candidate accessibility. A requested property that is not supplied by these sources, for example crime, mortality, observed illness, real-time occupancy, or parcel suitability, is out of scope for the current run. OASIS should suggest an official city/county or research source and require a reviewed data adapter, version, missing-value policy, and indicator direction before that property can enter analysis. It does not silently substitute another dataset.

Request interpretation is schema-first: one `analysis_goal` controls the executable workflow, `result_queries` holds independent read-only answers, `place_names` retains every detected study area for confirmation, and `ambiguities` records fields the model could not safely resolve. One run uses one confirmed city or county; analyze additional places separately and compare the saved runs.

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
|       |-- features/    # Task, run, result, and comparison interfaces
|       |-- store/       # Browser-session state
|       `-- styles/      # Global responsive styling
|-- tests/               # Unit and integration tests
|-- experiments/         # Research evaluation scripts
|-- notebooks/           # Minimal API-driven agent quickstart
```

See [PIPELINE.md](PIPELINE.md) for the computational workflow.

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
| [OpenStreetMap](https://www.openstreetmap.org/copyright) and [Nominatim](https://operations.osmfoundation.org/policies/nominatim/) | Candidate facilities and geocoding | Public service; follow usage policies |
| [openrouteservice](https://api.openrouteservice.org/) | Network matrices and point routes | [Create an account and API key](https://openrouteservice.org/sign-up/) |
| [Mapillary](https://www.mapillary.com/dashboard/developers) | Optional nearby street-level imagery | Register an app and copy its client token |
| [OpenRouter](https://openrouter.ai/settings/keys), [Google Gemini](https://aistudio.google.com/apikey), or [OpenAI](https://platform.openai.com/api-keys) | Language-model parsing and orchestration | OpenRouter with openai/gpt-4o is the default; create a provider key or configure local Ollama |

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
```

Put the server-default OpenRouter key in `OPENROUTER_API_KEY` inside the project-root `.env`. A visitor may instead enter a provider key in **Preliminary setup**; that key stays only in the browser-scoped backend session. Direct Gemini remains available through `GEMINI_API_KEY`, but access is evaluated from the backend network location and is limited to Google's [supported regions](https://ai.google.dev/gemini-api/docs/available-regions). If unavailable, select OpenRouter, OpenAI, or a local model instead; OASIS never switches providers silently. Gemini free-tier availability and quotas also depend on the selected model, account, and region. Only configure the services you use, and never commit the populated `.env`.

## Install

```powershell
conda create -n oasis-agent python=3.11
conda activate oasis-agent
pip install -r requirements.txt

cd frontend
npm install
cd ..
```

## Run the web application

Terminal 1:

```powershell
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Terminal 2:

```powershell
cd frontend
npm run dev
```

Open http://127.0.0.1:5173.

Basic workflow:

1. Optionally connect and test a visitor-provided language model.
2. Describe a supported city/county analysis question.
3. Confirm the interpreted task and place.
4. Review candidate types, travel mode, threshold, weights, and optional constraints.
5. Run and inspect Map, Analysis, Pareto, Compare, Report, and Provenance.

Example requests:

```text
Select 10 candidate cooling facilities in Phoenix within a 15-minute walk.
Compare efficiency and access inequality for 10 candidate facilities in Phoenix.
Which Phoenix tracts have the highest population-weighted heat exposure?
Which tracts are hardest to serve within 15 minutes?
I'm at the Lincoln Memorial. How can I find the nearest cooling facility?
```

Requests such as “Which NYC cooling centers are open right now?” are rejected with an official-directory or 311 alternative.

For Mapillary previews, add `MAPILLARY_ACCESS_TOKEN`, restart the backend, run an analysis, then click a selected facility. The backend endpoint is:

```text
/api/streetview?lat=<latitude>&lon=<longitude>&radius_m=100
```

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

## Verify

```powershell
python -m pytest tests -q

cd frontend
npm test
npm run build
npm run lint
```
