# OASIS 2026 -- Track B: Equity-Aware Public Health Facility Siting Agent

An end-to-end GeoAI agent for the ACM SIGSPATIAL 2026 Student Challenge,
Track B (Public Health & Spatial Equity): it takes a natural-language task,
autonomously acquires open geospatial data, solves an equity-weighted siting
optimization, and returns value judgments to human decision-makers through
human-in-the-loop mechanisms.

## Quick start

```bash
cp .env.example .env          # fill in API keys
pip install -r requirements.txt
# or: docker build -t oasis . && docker run -it oasis

# Mode 1: deterministic pipeline (no LLM -- reviewers' guaranteed repro path)
python main.py --mode pipeline --place "Los Angeles County, California, USA"

# outputs_run/: coverage_map.html, pareto.png, report.md

# Optional HITL web interface
streamlit run streamlit_app.py

# Mode 2: agent-driven (natural language in)
python main.py --mode agent --prompt "Site 10 cooling centers in Phoenix, prioritize elderly and low-income tracts"

Formal pipeline and manual tool-call guide: `PIPELINE.md`
# Unit tests (no external APIs)
pytest tests/ -q
```

Development notebook: `notebooks/explore.ipynb`; production runs do not depend on its state.

## Architecture

```
natural-language task
        v
   agent/                the ONLY layer that talks to an LLM
   |  orchestrator.py       parse -> plan -> execute w/ self-correction
   |  memory.py             Context: object handles + experience hints
   |  schemas.py            all cross-layer contracts (pydantic)
   |  prompts/*.md          prompts externalized; editable without touching code
        v  (tool names + args only -- never data values)
   tools/                deterministic, unit-testable, zero LLM calls
   |  registry.py           registration == exposure to the agent
   |  data_acquisition/     geocoding.py | census.py | osm.py
   |  spatial/              alignment.py | accessibility.py
   |  optimization/         models.py (MCLP) | pareto.py
        v
   robustness/           cross-cutting: retry.py | cache.py | fallback.py
        v
   hitl/                 value judgments return to humans
   |  elicitation.py        interactive preference clarification
   |  knobs.py              weight profiles: load + validate
        v
   output/               maps.py | charts.py | report.py
```

## File-to-review-criteria mapping

| Review dimension | Code | Paper section |
|---|---|---|
| Tool-Use Rigor & Autonomy | `agent/orchestrator.py`, `tools/registry.py` | §3 Architecture |
| Architectural Robustness | `robustness/`, `tools/spatial/alignment.py` | §4 Robustness |
| Social Good Alignment | `hitl/`, `tools/optimization/pareto.py` | §5 Value Alignment |
| Innovation & Reflection | `agent/memory.py`, provenance in `output/report.py` | §6 Discussion |

## Transferability

No hardcoded city exists anywhere. Changing the study area = changing one
`place_name` string: boundaries resolve via geocoding, and the metric
projection is auto-selected per study-area centroid (`tools/spatial/alignment.pick_metric_crs`).
Reviewers can name a new city during the live demo.
