"""Tiered fallback: live API -> disk cache -> bundled sample data.
The tier actually used is always reported honestly in Provenance."""
from __future__ import annotations
import logging, pathlib
from typing import Any, Callable
from agent.schemas import Provenance

log = logging.getLogger(__name__)
SAMPLE_DIR = pathlib.Path(__file__).resolve().parents[1] / "data" / "samples"


def with_sample_fallback(source: str, sample_file: str,
                         fetch: Callable[[], tuple[Any, str]]) -> tuple[Any, Provenance]:
    """Run the live fetch (which may itself hit cache); on failure fall back
    to a bundled sample file."""
    try:
        data, tier = fetch()
        return data, Provenance(source=source, tier=tier)
    except Exception as e:
        log.warning("%s live/cache failed (%s); falling back to sample", source, e)
        path = SAMPLE_DIR / sample_file
        if not path.exists():
            raise
        if path.suffix.lower() == ".csv":
            import pandas as pd
            data = pd.read_csv(path, dtype={"GEOID": "string"})
        else:
            import geopandas as gpd
            data = gpd.read_file(path)
        return data, Provenance(
            source=source, tier="sample",
            notes="Bundled sample data; results are for demonstration only")
