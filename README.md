# OASIS Agent

OASIS is an auditable autonomous GIS and LLM-GIS decision-support agent for evaluating access to existing cooling-facility candidates in the United States.

## Intended users and purpose

- Policy analysts and planners: compare candidate-facility portfolios under budget, travel-time, capacity, and equity constraints.
- Public-health teams: inspect population-weighted heat exposure, vulnerable-population coverage, and hard-to-reach areas.
- Academic researchers: run reproducible efficiency-Gini, mobility, vulnerability, and threshold sensitivity analyses with provenance.

OASIS is not a consumer navigation service. It does not verify which facility is open right now, provide emergency advice, search vacant land, or make zoning decisions.

## Main capabilities

- Natural-language routing: full analysis, quick diagnostic, clarification, or out of scope.
- US city/county resolution with explicit confirmation.
- ACS/TIGER demand data, population-weighted Landsat LST, environmental diagnostics, and OSM/uploaded candidate facilities.
- Walking, cycling, and driving accessibility using ORS, with bounded Haversine preview/fallback.
- Gradual coverage, explicit assignment, optional capacity, population-weighted Gini, and interactive Pareto solutions.
- Vulnerability preprocessing: missingness, direction, normalization, correlation, VIF, PCA, alternative weights, and robustness scans.
- MapLibre maps, point/address route planning, route heat audit, and optional Mapillary imagery.
- Reports, charts, provenance, and notify-only source-version checks.
- OpenAI-compatible BYOK for OpenAI, OpenRouter, approved custom endpoints, and local Ollama/vLLM.

Candidate facilities are possible existing service locations, not confirmed cooling centers. Unknown opening hours, fees, access, capacity, and official status remain explicit.

## Code layout

```text
React UI -> FastAPI -> OasisService -> bounded LLM orchestration
                                  -> deterministic GIS tools
                                  -> data, optimization, maps, reports, provenance

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
```

See [PIPELINE.md](PIPELINE.md) for the computational workflow.

## Requirements

- Python 3.11
- Node.js 22+
- GDAL-compatible geospatial libraries
- CBC solver for PuLP
- Internet access for live data and routing

Copy the environment template:

```powershell
Copy-Item .env.example .env
```

Relevant keys:

```dotenv
OPENROUTER_API_KEY=
ORS_API_KEY=
CENSUS_API_KEY=
MAPILLARY_ACCESS_TOKEN=
```

Mapillary is optional. Register an application at https://www.mapillary.com/dashboard/developers and copy its client access token. Never commit the populated `.env`.

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

## Outputs

Web runs are stored under `outputs_run/api_runs/<run_id>/`. Outputs include MapLibre GeoJSON, charts, Markdown reports, provenance, progress, and run records.

## Deployment and mobile

```powershell
docker compose up --build
```

A phone is a browser client; data processing, optimization, API keys, and optional local models remain on a reachable backend. Local mobile testing requires the computer to stay on and be reachable over LAN or a secure tunnel. Public use requires a hosted backend/frontend.

## Verify

```powershell
python -m pytest tests -q

cd frontend
npm test
npm run build
npm run lint
```
