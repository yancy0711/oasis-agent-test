# Formal Pipeline and Agent Flow

This document describes the maintained production workflow.
The production code does not read Notebook variables or execution state.

## 1. Environment and verification

Activate the project environment from the repository root and run:

```powershell
conda activate oasis-agent
pytest tests -q
python -m cli.main --mode pipeline --place "Los Angeles County, California, USA"
python -m cli.main --mode pipeline --place "San Francisco, California, USA" --profile cooling_first
python -m cli.main --mode pipeline --place "New York" --n-facilities 12 --threshold-minutes 10
```

`pytest.ini` sets `pythonpath = .`, so both commands below resolve the local
`oasis`, `api`, and `cli` packages in the same way:

```powershell
pytest tests -q
python -m pytest tests -q
```

A successful pipeline run writes:

```text
outputs_run/<place_slug>/coverage_map.geojson
outputs_run/<place_slug>/pareto.png
outputs_run/<place_slug>/report.md
```

## 2. Deterministic pipeline entry

`cli.main.run_pipeline(cfg, place, profile=None, ..., n_facilities=None, threshold_minutes=None)` performs four small actions:

1. Read facility count, accessibility threshold, and value profile from `cfg`.
2. Build a complete `TaskSpec`; `optimization.value_profile` defaults to `balanced`.
3. Resolve one unique US boundary or require HITL confirmation before any data tool runs.
4. Call `oasis.services.workflow.run_tool_chain(cfg, task)`.

The four valid profiles are `efficiency_first`, `equity_first`, `cooling_first`, and `balanced`; `--profile` overrides the YAML value for one CLI run.

The function returns a dictionary. Check `result["ok"]` before reading
`result["solution"]` or `result["outputs"]`.

```python
from cli.main import load_cfg, run_pipeline

result = run_pipeline(load_cfg(), "Los Angeles County, California, USA")
if not result["ok"]:
    failed = result["results"][-1]
    raise RuntimeError(f"{result['failed_tool']}: {failed.error.message}")
print(result["solution"].selected_ids)
print(result["outputs"])
```

## 3. Baseline and diagnostic tools

The deterministic pipeline keeps its fixed seven-tool baseline. The agent can additionally select diagnostic tools that match the confirmed question. Large GeoDataFrames and NumPy arrays never enter an LLM prompt. Each tool
stores them in `Context` under a stable handle and returns a lightweight
`ToolResult`.

| Step | Registered tool | Reads | Stores | Main work |
|---:|---|---|---|---|
| 1 | `resolve_study_area` | `place_name` | `study` | OSM boundary and Census state/county FIPS |
| 2 | `prepare_demand` | `study` | `demand` | ACS/TIGER, WorldPop-weighted Landsat heat, and vulnerability diagnostics |
| 3 | `prepare_built_environment` | `demand` | `demand`, `built_environment` | Landsat NDVI, NDBI, albedo, and an auditable passive-environment burden diagnostic |
| 4 | `fetch_candidate_sites` | `study` | `candidates` | OSM library/community-centre/school points |
| 5 | `build_travel_matrix` | `demand`, `candidates` | `travel` | ORS or declared Haversine travel minutes plus diagnostics |
| 6 | `optimize_sites` | `demand`, `candidates`, `travel` | `analysis` | Gradual coverage/allocation, optional capacity, and efficiency-Gini sweep |
| 7 | `generate_outputs` | `study`, `demand`, `candidates`, `analysis`, `built_environment` | `outputs` | MapLibre GeoJSON, Pareto chart, and Markdown report |
The additional agent-selectable tools are:

| Tool | Question answered |
|---|---|
| `validate_constraints` | Are global and regional count requirements arithmetically consistent? |
| `load_candidate_sites` | Use a reviewed candidate CSV instead of OpenStreetMap candidates. |
| `summarize_heat_exposure` | Which prepared tracts have the highest heat exposure without selecting facilities? |
| `find_minimum_facilities` | At a fixed threshold, what is the LSCP facility lower bound? |
| `find_minimum_threshold` | At a fixed budget, what is the smallest matrix-supported feasible threshold? |
| `analyze_reachability` | Which tracts are hardest, unserved, or structurally unreachable? |
| `answer_result_queries` | Answer one or more typed, read-only tract questions and publish formula, terms, parameters, source fields, and top rows. |
| `run_sensitivity_analysis` | How do confirmed counts and thresholds change coverage, Gini, and travel outcomes? |
| `compare_scenarios` | How do the last two successful optimization scenarios differ? |
| `compare_value_profiles` | How do two or more confirmed value profiles differ on one shared spatial baseline? |

### Step 1: resolve the study area

```python
registry.get("resolve_study_area").fn(
    ctx=ctx,
    place_name="Los Angeles County, California, USA",
)
```

`resolve_fips()` obtains the state and a representative county from the OSM boundary.
For city boundaries, cached TIGER county polygons then identify every materially
overlapping county, so multi-county cities such as New York load all relevant tracts.
Responses are cached under `data/cache/places/<place_slug>/`.

Every source cache uses the same place scope, for example:

```text
data/cache/places/los_angeles_county_california_usa/<source>/v1/
data/cache/places/san_francisco_california_usa/<source>/v1/
```

Runtime cache writes require a concrete place scope; unscoped calls raise instead of creating `_shared`. Ambiguous abbreviations such as `LA` are never assigned a cache scope until the user confirms a boundary.
Cache identity comes from the selected Nominatim boundary metadata, not the raw input string. Exact query hashes are checked first; a short name may reuse one uniquely matching cached canonical locality. Thus `San Francisco` and `San Francisco, California, USA` share `san_francisco_california_usa`. A new ambiguous spelling may require one geocoder lookup, but downstream Census, heat, OSM, and ORS caches still use the canonical scope.

The Nominatim request is restricted with `countrycodes=us`, and `USA`, `U.S.A.`, `United States`, and `United States of America` are optional terminal suffixes. A unique boundary is accepted automatically; multiple city/county/state candidates require interactive confirmation before Census, OSM, heat, or ORS work begins. Candidate ordering may propose a city first, but no abbreviation has a hard-coded destination.

### Step 2: prepare tract demand

```python
registry.get("prepare_demand").fn(ctx=ctx)
```

This step joins ACS rows to TIGER polygons by `GEOID`, rejects a join rate at
or below 95%, merges Landsat LST and optical environment diagnostics, calculates
WorldPop-weighted tract heat where raster coverage is sufficient, calculates
population density in a metric CRS, and builds tract centroid demand points. The configured vulnerability
pipeline then applies explicit missing-value and direction rules, percentile
normalization, correlation/VIF and PCA diagnostics, and marks the top quartile
of the active composite score as priority tracts.

### Step 3: prepare built-environment diagnostics

```python
registry.get("prepare_built_environment").fn(ctx=ctx)
```

This step derives tract-level Landsat NDVI, NDBI, and albedo where imagery is available, then records a transparent passive-environment burden diagnostic. If imagery cannot be acquired, it records an explicit neutral fallback and provenance rather than inventing spatial variation.

### Step 4: fetch candidate facilities

```python
registry.get("fetch_candidate_sites").fn(
    ctx=ctx,
    amenities=["library", "community_centre", "school"],
)
```

An empty OSM result is returned as a structured `ToolError`, allowing the
agent to repair the amenity list without receiving raw geospatial objects.

### Step 5: build accessibility

```python
registry.get("build_travel_matrix").fn(ctx=ctx)
```

When `router: ors`, deterministic guards run before the production sparse-routing path:

1. City and county boundaries may enter sparse ORS checks when `ors_city_only: false`.
2. Areas above `ors_max_area_km2` (default 15,000 km2) use Haversine and print a warning.
3. Build the complete Haversine fallback and an optimistic lower bound.
4. Keep pairs within `20 * 1.5 = 30` minutes as ORS-eligible.
5. Eligible-pair counts above `ors_max_eligible_pairs` use Haversine and print a warning.
6. If tiled planning exceeds `ors_max_requests`, safely repack a dense matrix when the added route work stays within `ors_max_block_route_expansion`; otherwise retain Haversine with an explicit warning.
7. Read eligible pairs from `data/cache/places/<place_slug>/ors/v1/pair_durations.sqlite`.
8. Request ORS blocks only for missing pairs; a complete cache yields `live_pairs=0`.
9. Print in-place progress bars for the cache scan and live ORS request blocks.

Every fallback reason and the actual cache/live tier are recorded in provenance.

### Step 6: optimize and sweep trade-offs

```python
registry.get("optimize_sites").fn(
    ctx=ctx,
    n_facilities=10,
    threshold_minutes=15,
    weights=task.weights.model_dump(),
)
```

The tool uses gradual network coverage and explicit tract-to-site assignment,
with optional facility capacity. It evaluates population-weighted access
efficiency against equality \\(1-G_P\\) over an 11-point \\(\\alpha\\) sweep.
It stores the requested solution, all sweep points, and the unique non-dominated
frontier under `analysis`. The web explorer may inspect and map-preview a point;
only explicit confirmation creates a revision.

Point routes are a separate post-optimization audit. ORS first selects the
minimum-network-time site among the optimized sites and returns route geometry.
Tract-intersection length weighting then reports route LST and available
environment indices. This audit does not alter ORS routing or the siting objective.

### Step 7: generate the three outputs

```python
registry.get("generate_outputs").fn(ctx=ctx, output_dir="outputs_run")
```

The report uses only solver results, the clarified `TaskSpec`, and accumulated
`Provenance` records.

## 4. Manual seven-tool execution

This is the non-LLM equivalence check for `run_pipeline()`:

```python
from oasis.agent.context import Context
from oasis.contracts.domain import TaskSpec
from oasis.settings import get_weights
from cli.main import load_cfg
from oasis.services.registry import registry

cfg = load_cfg()
task = TaskSpec(
    place_name="Los Angeles County, California, USA",
    n_facilities=cfg["optimization"]["n_facilities"],
    threshold_minutes=cfg["accessibility"]["threshold_minutes"],
    weights=get_weights("balanced"),
)
ctx = Context()
ctx.put("config", cfg)
ctx.put("task", task)

calls = [
    ("resolve_study_area", {"place_name": task.place_name}),
    ("prepare_demand", {}),
    ("prepare_built_environment", {}),
    ("fetch_candidate_sites", {"amenities": task.candidate_amenities}),
    ("build_travel_matrix", {}),
    ("optimize_sites", {
        "n_facilities": task.n_facilities,
        "threshold_minutes": task.threshold_minutes,
        "weights": task.weights.model_dump(),
    }),
    ("generate_outputs", {"output_dir": "outputs_run"}),
]

for name, args in calls:
    result = registry.get(name).fn(ctx=ctx, **args)
    print(name, result.ok, result.summary)
    if not result.ok:
        break
```

The automated version is `test_cli_pipeline_matches_manual_seven_tool_chain()`
in `tests/integration/test_pipeline_agent.py`. It runs both paths on the same synthetic geospatial
data and compares the complete serialized `SiteSolution` objects.

## 5. Agent entry

`cli.main.run_agent()` validates the task through HITL and then runs a bounded observation-action loop:

```text
user prompt
  -> parse against the TaskSpec JSON Schema
  -> retain one primary analysis_goal, zero or more result_queries, all place_names, and typed ambiguities
  -> confirm exactly one place and unresolved value judgments
  -> model selects one registered action
  -> deterministic tool returns a compact ToolResult
  -> model observes the result and selects the next action
  -> finish only when the primary handle and every result query are complete; otherwise bounded repair or HITL
```

`n_facilities` controls the decision budget; each `ResultQuery.limit` independently controls answer length. A single run owns one study boundary. Multiple extracted places require a user choice and should be run separately before comparison. `max_agent_steps` bounds total decisions, and `max_self_correction_rounds` bounds local retries. Repeating an identical failed call is blocked. Large data objects stay in `Context`; the model receives only handle names and compact summaries.

For an infeasible fixed-budget request, the model can select LSCP, minimum-threshold search, or reachability analysis instead of silently changing a confirmed constraint.

```powershell
python -m cli.main --mode agent --prompt "At a strict 15-minute threshold, how many Phoenix cooling centers are required for full tract coverage?"
```

## 6. Offline and live agent tests

Offline `FakeLLM` tests return one action at a time:

```python
fake = FakeLLM([
    {"place_name": "Phoenix, Arizona, USA", "weights": None},
    {"kind": "tool", "tool": "capture_task_test"},
    {"kind": "finish", "message": "Task complete."},
])
```

Run all deterministic tests with `pytest tests -q`. Run `python -m experiments.evaluate_agent_behavior` to evaluate live model branching, diagnostic tool selection, and self-correction; this command uses the configured language-model provider.
