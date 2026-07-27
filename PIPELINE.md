# Formal Pipeline and Agent Flow

This document describes the production path extracted from `notebooks/explore.ipynb`.
The production code does not read Notebook variables or execution state.

## 1. Environment and verification

Activate the project environment from the repository root and run:

```powershell
conda activate oasis-agent
pytest tests -q
python main.py --mode pipeline --place "Los Angeles County, California, USA"
python main.py --mode pipeline --place "San Francisco, California, USA" --profile cooling_first
python main.py --mode pipeline --place "New York" --n-facilities 12 --threshold-minutes 10
```

`pytest.ini` sets `pythonpath = .`, so both commands below resolve the local
`agent` and `tools` packages in the same way:

```powershell
pytest tests -q
python -m pytest tests -q
```

A successful pipeline run writes:

```text
outputs_run/<place_slug>/coverage_map.html
outputs_run/<place_slug>/pareto.png
outputs_run/<place_slug>/report.md
```

## 2. Deterministic pipeline entry

`main.run_pipeline(cfg, place, profile=None, ..., n_facilities=None, threshold_minutes=None)` performs four small actions:

1. Read facility count, accessibility threshold, and value profile from `cfg`.
2. Build a complete `TaskSpec`; `optimization.value_profile` defaults to `balanced`.
3. Resolve one unique US boundary or require HITL confirmation before any data tool runs.
4. Call `tools.workflow.run_tool_chain(cfg, task)`.

The four valid profiles are `efficiency_first`, `equity_first`, `cooling_first`, and `balanced`; `--profile` overrides the YAML value for one CLI run.

The function returns a dictionary. Check `result["ok"]` before reading
`result["solution"]` or `result["outputs"]`.

```python
from main import load_cfg, run_pipeline

result = run_pipeline(load_cfg(), "Los Angeles County, California, USA")
if not result["ok"]:
    failed = result["results"][-1]
    raise RuntimeError(f"{result['failed_tool']}: {failed.error.message}")
print(result["solution"].selected_ids)
print(result["outputs"])
```

## 3. Six registered tools

Large GeoDataFrames and NumPy arrays never enter an LLM prompt. Each tool
stores them in `Context` under a stable handle and returns a lightweight
`ToolResult`.

| Step | Registered tool | Reads | Stores | Main work |
|---:|---|---|---|---|
| 1 | `resolve_study_area` | `place_name` | `study` | OSM boundary and Census state/county FIPS |
| 2 | `prepare_demand` | `study` | `demand` | ACS demographics, TIGER tracts, Landsat heat, centroids, indicators |
| 3 | `fetch_candidate_sites` | `study` | `candidates` | OSM library/community-centre/school points |
| 4 | `build_travel_matrix` | `demand`, `candidates` | `travel` | ORS or declared Haversine travel minutes plus diagnostics |
| 5 | `optimize_sites` | `demand`, `candidates`, `travel` | `analysis` | Requested MCLP solution and coverage Pareto sweep |
| 6 | `generate_outputs` | `study`, `demand`, `candidates`, `analysis` | `outputs` | HTML map, PNG chart, Markdown report |

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
or below 95%, merges Landsat LST, calculates population density in a metric
CRS, builds tract centroid demand points, and marks the top poverty quartile
as vulnerable.

### Step 3: fetch candidate sites

```python
registry.get("fetch_candidate_sites").fn(
    ctx=ctx,
    amenities=["library", "community_centre", "school"],
)
```

An empty OSM result is returned as a structured `ToolError`, allowing the
agent to repair the amenity list without receiving raw geospatial objects.

### Step 4: build accessibility

```python
registry.get("build_travel_matrix").fn(ctx=ctx)
```

When `router: ors`, deterministic guards run before the Notebook sparse path:

1. City and county boundaries may enter sparse ORS checks when `ors_city_only: false`.
2. Areas above `ors_max_area_km2` (default 15,000 km?) use Haversine and print a warning.
3. Build the complete Haversine fallback and an optimistic lower bound.
4. Keep pairs within `20 * 1.5 = 30` minutes as ORS-eligible.
5. Eligible-pair counts above `ors_max_eligible_pairs` use Haversine and print a warning.
6. Planned requests above `ors_max_requests` use Haversine and print a warning.
7. Read eligible pairs from `data/cache/places/<place_slug>/ors/v1/pair_durations.sqlite`.
8. Request ORS blocks only for missing pairs; a complete cache yields `live_pairs=0`.
9. Print in-place progress bars for the cache scan and live ORS request blocks.

Every fallback reason and the actual cache/live tier are recorded in provenance.

### Step 5: optimize and sweep trade-offs

```python
registry.get("optimize_sites").fn(
    ctx=ctx,
    n_facilities=8,
    threshold_minutes=15,
    weights=task.weights.model_dump(),
)
```

The tool solves the requested weighted MCLP and an 11-point direct coverage
sweep. It stores the selected solution, all sweep points, and the unique
non-dominated frontier under `analysis`.

### Step 6: generate the three outputs

```python
registry.get("generate_outputs").fn(ctx=ctx, output_dir="outputs_run")
```

The report uses only solver results, the clarified `TaskSpec`, and accumulated
`Provenance` records.

## 4. Manual six-tool execution

This is the non-LLM equivalence check for `run_pipeline()`:

```python
from agent.memory import Context
from agent.schemas import TaskSpec
from hitl.knobs import get_weights
from main import load_cfg
from tools.registry import registry

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

The automated version is `test_cli_pipeline_matches_manual_six_tool_chain()`
in `tests/test_pipeline_agent.py`. It runs both paths on the same synthetic geospatial
data and compares the complete serialized `SiteSolution` objects.

## 5. Agent entry

`main.run_agent()` executes this order:

```text
user prompt
  -> Orchestrator.parse_task()
  -> hitl.clarify_place()  # deterministic candidate validation + ambiguity HITL
  -> hitl.clarify()
  -> Context.put("task", task)
  -> Orchestrator.plan()
  -> Orchestrator.execute(step) for every PlanStep
  -> registered workflow tools
  -> outputs and provenance
```

The LLM only returns `TaskSpec`, `AgentPlan`, or a repaired `PlanStep`. It does
not receive or mutate DataFrames, geometry, travel matrices, or solver values.

Live example:

```powershell
python main.py --mode agent --prompt "Site 6 cooling centers in Bakersfield, California, prioritize elderly residents"
```

Programmatic non-interactive example:

```python
from main import load_cfg, run_agent

result = run_agent(
    load_cfg(),
    "Site 6 cooling centers in Bakersfield, California",
    interactive=False,
)
print(result["task"])
print(result["plan"])
print([item.ok for item in result["results"]])
```

`interactive=False` applies the documented balanced profile only when the
parsed task leaves `weights=None`; the assumption is appended to `task.notes`.

## 6. Offline FakeLLM flow

`tests/test_pipeline_agent.py` supplies canned dictionaries through `FakeLLM`:

```python
fake = FakeLLM([
    {"place_name": "Phoenix, Arizona, USA", "weights": None},
    {"steps": [{"tool": "capture_task_test", "args": {}}]},
])
result = run_agent(cfg, "help underserved Phoenix", llm=fake, interactive=False)
```

This verifies parse, HITL, planning, execution, and self-correction without an
OpenRouter call or API cost.

## 7. Files changed and purpose

| File | Purpose |
|---|---|
| `pytest.ini` | Make direct `pytest` imports deterministic on Windows/Conda |
| `tools/data_acquisition/geocoding.py` | Implement cached place-to-FIPS resolution |
| `tools/workflow.py` | Define and register the shared six-tool production workflow |
| `robustness/cache.py` | Isolate every data-source cache by normalized place name |
| `tools/registry.py` | Load the six tools when the registry is imported |
| `main.py` | Implement deterministic and agent CLI entry points |
| `agent/memory.py` | Provide one Context object store and multi-record lineage API |
| `agent/schemas.py` | Remove mutable defaults from cross-layer contracts |
| `agent/orchestrator.py` | Use the shared Context and execute the bounded repair loop |
| `tests/test_pipeline_agent.py` | Cover FIPS, provenance, FakeLLM, repair, agent flow, and equivalence |
| `tests/test_place_cache.py` | Cover place ambiguity, scoped caches, and ORS boundary guards |
| `requirements.txt` | Declare the existing `pygris` dependency |
