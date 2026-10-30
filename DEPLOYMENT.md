# Local Docker deployment and Mac migration

This guide runs the current web application on one machine. It keeps the existing research model and resident workflow separate. Docker packages the application; live LLM, geocoding, Overpass, Census, imagery, and ORS availability still depend on their providers and network.

## 1. Save this version from Windows

The current working branch is `demo-optimization`. Review the changes before committing:

```powershell
Set-Location 'D:\Research\ACM SIGSPATIAL\oasis-agent'
git branch --show-current
git status --short
git diff --check
git check-ignore .env
git add -A
git diff --cached --stat
git diff --cached --name-only
```

The staged list should include `.env.example`, deployment files, source, tests, and documentation. It must not include your populated `.env`, cached data, generated outputs, or local skills. If `.env` was previously tracked, remove it from the index with `git rm --cached -- .env`; the local file stays in place. Then:

```powershell
git commit -m "Improve demo reliability and Mac Docker deployment"
git push -u origin demo-optimization
```

Use `git remote get-url origin` to obtain your repository URL for cloning. Transfer actual keys privately or populate them again on the Mac; do not upload `.env` to GitHub.

## 2. Prepare the Mac

Install and open [Docker Desktop for Mac](https://docs.docker.com/desktop/setup/install/mac-install/), choosing the installer for Apple Silicon or Intel. Wait until its engine is running. Native Python, Conda, Node, and GDAL are not needed to run these containers.

```bash
git clone --branch demo-optimization https://github.com/yancy0711/oasis-agent-test.git oasis-agent
cd oasis-agent
cp .env.example .env
open -e .env
mkdir -p data/cache outputs_run tmp/uploads
docker version
docker compose version
```

Fill in the credentials you use: the LLM provider selected in `config/settings.yaml` (currently OpenRouter), `ORS_API_KEY`, and `CARTO_BASEMAP_API_KEY`. Census and Mapillary credentials enable their respective integrations. A provider key alone does not change the configured provider/model. Public Overpass and Nominatim need no key; the current hosted setup needs both `OASIS_OVERPASS_URL=https://api.overpass.ch/v1/interpreter` and `OVERPASS_CH_API_KEY`. Copy these privately from your Windows `.env`, or enter them manually. Existing valid keys can be reused on the Mac; switching computers does not itself require new accounts or keys. Check any account, origin, or IP restrictions in the provider dashboard.

The runtime already uses `https://api.heigit.org/openrouteservice/v2/matrix`,
and derives the matching directions URL. The [official ORS migration notice](https://ask.openrouteservice.org/t/deprecating-api-openrouteservice-org-in-favour-of-api-heigit-org/7912)
says existing API keys also work at the new address. The [latest announcement](https://heigit.org/changes-in-the-openrouteservice-universe/)
dated 22 October 2026 schedules the old address for shutdown in the week of
16 November; older notices give earlier dates, so use the new address now.
The source monitor links to the official status page; it does not treat the
obsolete public `/health` endpoint as evidence of routing failure.

Compose sets container paths to `/app/config/settings.yaml`, `/app/data/cache`, and `/app/outputs_run`; it does not reuse Windows drive paths from `.env`. Application settings and value profiles remain in `config/*.yaml`; credentials remain in `.env`.

## 3. Build, start, and check

```bash
docker compose up --build -d
docker compose ps
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:5173/api/health
docker compose logs --tail=100 backend frontend
```

Open **http://127.0.0.1:5173**. The first build downloads base images, Linux packages, and Python/Node dependencies; it can take several minutes. Run it before the demonstration.

The frontend image builds React and serves it through Nginx. `/api/` is proxied to FastAPI on the Compose network. The backend uses one Uvicorn worker so its in-memory cancellation controls and resident sessions stay consistent. Successful data and outputs persist in the mounted host directories; uploaded CSVs persist in `tmp/uploads/`.

The images do not force `linux/amd64`: Docker builds for the local platform. Debian installs native `coinor-cbc`, which the solver now prefers over PuLP's bundled executable. The backend build includes a small CBC solve and fails if that binary cannot execute. No optimization objective or constraint changes for Mac. See [Docker multi-platform builds](https://docs.docker.com/build/building/multi-platform/) and [PuLP solver configuration](https://github.com/coin-or/pulp/blob/master/doc/source/guides/how_to_configure_solvers.rst).

Different CBC versions can choose different tied optima or make different progress within a time limit. Check feasibility, objective values, metrics, and reported solver status on the Mac; identical facility IDs across platforms are not guaranteed.

## 4. Update configuration or code

### Fast recovery when the page reports 502 or stops responding

A successful rebuild also restarts services, so recovery after a rebuild does
not prove that rebuilding was necessary. Check both network hops first:

```bash
docker compose ps -a
curl --max-time 3 --fail http://127.0.0.1:8000/health
curl --max-time 3 --fail http://127.0.0.1:5173/api/health
docker compose logs --tail=80 frontend backend
```

If port 8000 works but the proxied health check fails, restart only Nginx:

```bash
docker compose restart frontend
```

If the backend is stopped or unreachable, first save the logs above, then:

```bash
docker inspect "$(docker compose ps -a -q backend)" --format 'status={{.State.Status}} oom={{.State.OOMKilled}} exit={{.State.ExitCode}} restarts={{.RestartCount}}'
docker compose restart backend
docker compose restart frontend
```

A backend restart interrupts active jobs and clears in-memory LLM/resident
sessions; reconnect the model and rerun the task as needed. Disk caches and
outputs remain. If `oom=true`, review Docker Desktop memory allocation and
close competing work before repeating a large analysis. If both health checks
work, diagnose the particular LLM/OSM/ORS error instead of restarting the app.
If Docker itself is unavailable after sleep, resume/start Docker Desktop, then
run `docker compose up -d --no-build`.

Nginx now uses Docker DNS to refresh the backend address every five seconds.
Both services use `restart: unless-stopped`; this restarts an exited process,
not an unhealthy-but-running process, and does not undo a manual stop. BLAS
threads are bounded to reduce resource multiplication in Pareto worker processes.
The lightweight `/health` endpoint does not wait for the synchronous API worker
pool. These measures do not guarantee third-party provider availability.

For the demonstration, keep the Mac awake and connected to power, pre-run the
main cases, and avoid pulling/rebuilding during the live session. Use
`caffeinate -i` in a separate open terminal to prevent idle sleep.

After changing only `.env`, recreate the backend and restart Nginx so it resolves the backend's current container address:

```bash
docker compose up -d --force-recreate backend
docker compose restart frontend
```

Reload the browser and repeat the model connection check. Environment variables are supplied at container creation, not baked into the image; see [Compose environment configuration](https://docs.docker.com/compose/how-tos/environment-variables/set-environment-variables/).

After pulling source or changing YAML configuration (copied into the backend image):

```bash
git pull --ff-only
docker compose up --build -d
docker compose restart frontend
```

Stop with `docker compose down`. Bind-mounted caches, results, and uploads remain on the Mac. A standalone `docker build` only creates one image: it does not configure and run both services, load runtime secrets, or mount persistent data. Use Compose for this setup.

For a model server running on the Mac host, container `localhost` points to the container itself. Use the appropriate `http://host.docker.internal:<port>/...` endpoint, and select/configure that local provider explicitly. See [Docker Compose networking](https://docs.docker.com/compose/how-tos/networking/).

## 5. Transfer data and prepare the demonstration

### Automatic ORS matrix budget

`accessibility.ors_max_policy_threshold_minutes` defaults to **60 minutes**.
This is a local screening horizon, not an ORS provider restriction. Larger
thresholds can require more network pairs; the pair and request guards still
apply. If network refinement cannot finish, the computed Haversine answer,
map, and report remain available, with the reason under ORS refinement details.

`accessibility.ors_max_eligible_pairs: 250000` is a local guard for **auto**
refinement, not ORS's per-request cap. Keep it as the demonstration default.
For an offline rehearsal of the 311,869-pair LA case, `350000` permits that case
to proceed; it does not guarantee speed or completion. Make the YAML edit,
rebuild the backend, and measure one complete run before choosing this setting
for the presentation. Do not increase concurrency or per-request route limits
to compensate. ORS plan quotas, the separate 500-request application guard,
and provider response times still apply.

At the configured 3,500-route cap, 311,869 uncached pairs require at least
90 requests; sparse blocks and the 1,750-route target can require more.
With the current 1.6-second request spacing, even the ideal count represents
over two minutes of scheduled request starts, before the final response and
optimization. This is an estimate, not a measured runtime. Inspect actual
`planned_requests`, `cache_pairs`, and `live_pairs` in the routing report.
Selecting fewer final facilities does not shrink the candidate matrix.

The auto preflight runs before the precise matrix cache lookup, so cached data
does not bypass this guard. Keep the rehearsed limit if that large case is
part of the demo. Explicit precise mode skips this particular auto guard,
but retains request budgets and provider limits.

### Move compatible caches privately

Without copying caches, a fresh Mac clone starts cold and rebuilds its local
cache while running. Existing compatible data caches can be copied; it is not
necessary to download everything again. Stop the Windows backend first
(`Ctrl+C` in its terminal, or `docker compose down`) so SQLite/WAL and other
cache files are not changing, then archive:

```powershell
Set-Location 'D:\Research\ACM SIGSPATIAL\oasis-agent'
New-Item -ItemType Directory -Force tmp | Out-Null
tar -czf tmp/oasis-cache.tar.gz -C data cache
```

Transfer `tmp/oasis-cache.tar.gz` to the Mac's Downloads folder using a private
file transfer. In the Mac repository, before starting the backend:

```bash
mkdir -p data/cache outputs_run tmp/uploads
tar -xzf "$HOME/Downloads/oasis-cache.tar.gz" -C data
```

Keep the directory structure intact. Matrix `.npz` files, routing SQLite files,
OSM responses and geospatial data can be reusable across Windows and Linux.
Cache keys, endpoints, profiles, coordinates, versions and expiry still determine
reuse. Research ORS matrices currently expire after 30 days; resident POI data
after 24 hours. A cache copy is not an offline bundle and contains no portable
browser session. Run the main cases on the Mac once to verify actual cache hits.
Do not overwrite a Mac's active caches while its backend is running.

### Repeated Overpass timeouts: change the service

**Retry same task** in the recovery panel creates a new run with the server's
original confirmed parameters and mode, reusing compatible successful caches.
**Edit search settings** opens the analysis settings without altering them.
**Use candidate CSV** explicitly selects CSV in the editor; a reviewed upload
is then required before running. No source or search scope is changed automatically.

If both public instances repeatedly fail, a compatible independent hosted or
self-hosted Overpass instance can replace them. `overpass.kumi.systems` is the
former name of Private.coffee, so it is not an independent fallback. See the
[OSM instance list](https://wiki.openstreetmap.org/wiki/Overpass_API#Public_Overpass_API_instances).

For example, [overpass.ch documents](https://overpass.ch/docs) a compatible
interpreter URL after creating an account and key. Put the populated value only
in the backend `.env`:

```dotenv
OASIS_OVERPASS_URL=https://api.overpass.ch/v1/interpreter
OVERPASS_CH_API_KEY=YOUR_KEY
```

Remove `RESIDENT_OVERPASS_URL` if you want resident searches to use this global
setting too; otherwise it overrides the global setting for resident discovery.
The key is sent in `X-API-Key` only to `api.overpass.ch`; it is not part of the
request URL. The provider's key-in-path URLs remain compatible with older setups.
An explicit endpoint replaces the default public fallback list. Restart native
Uvicorn, or for Docker run:

```bash
docker compose up -d --force-recreate backend
docker compose restart frontend
```

Then retry a small US lookup and the actual demonstration query. An account/key
and provider limits still apply. Authenticated checks on 2026-10-25 completed the
Riverside resident search and a detailed route. The LA research acquisition also
completed after switching the hosted query to bounding-box prefiltering with the
original exact boundary filter retained locally: 2,642 raw facilities in about
14 seconds, then about 1.1 seconds from local cache. These are observed results,
not fixed future counts or a completed rerun of the full research pipeline.
The independently listed
VK Maps endpoint returned HTTP 504 in a small live probe, so it was not installed
as an additional automatic fallback. Increasing timeouts or adding more public
servers cannot guarantee service availability.

- Git clone does not include `.env`, `data/cache/`, `outputs_run/`, or uploaded CSVs. Optionally copy `data/cache/` privately after cancelling jobs and stopping the old backend. Compatible successful cache entries can reduce acquisition time; normal cache validity and provider-specific expiry still apply.
- Existing Windows run records contain absolute artifact paths. Copying `outputs_run/` does **not** migrate those saved runs reliably to Linux. Run the main demonstrations again on the Mac; keep exported reports/charts or a clearly labelled recording separately as presentation materials. Browser sessions and in-memory resident search IDs also do not migrate.
- Pre-run Riverside, Los Angeles, and Phoenix on the actual Mac and presentation network. Confirm one research result, one resident search, one point route, and cancellation/retry. Warm data does not eliminate every live external request or guarantee provider uptime.
- Use an explicit US starting point when presenting outside the US. Browser location needs user permission; the Mac's current location may be outside the supported range.

## 6. Optional development checks on macOS

For frontend development/testing outside Docker, install Node 22+ and run:

```bash
cd frontend
npm ci
npx playwright install chromium
npm test
npm run build
npm run lint
npm run test:e2e
```

Playwright uses Chromium on macOS/Linux and Edge on Windows. Its API-mocked scenarios check interaction and recovery, not live provider health. Backend development tests additionally require the Python dependencies and CBC described in the README.

Validation limit for this revision: Compose configuration and the local automated suites can be checked on Windows. Docker Desktop's engine was not running during this maintenance session, so a complete Linux image build and an Apple Silicon run still need to be checked on the Mac using step 3.
