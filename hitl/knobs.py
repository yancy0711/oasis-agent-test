"""Configurable value-weight knobs (official HITL mechanism #2):
load, validate, and hand out weight profiles defined in
config/value_profiles.yaml."""
from __future__ import annotations
import pathlib
import yaml
from agent.schemas import ValueWeights

PROFILES_PATH = pathlib.Path(__file__).resolve().parents[1] / "config" / "value_profiles.yaml"


def load_profiles(path: pathlib.Path = PROFILES_PATH) -> dict[str, dict]:
    """Load named profiles and reject weights that do not sum to one."""
    profiles = yaml.safe_load(path.read_text(encoding="utf-8"))["profiles"]
    for name, p in profiles.items():   # validate on load, fail fast
        total = sum(p["weights"].values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"profile '{name}' weights sum to {total}, expected 1.0")
    return profiles


def get_weights(name: str) -> ValueWeights:
    """Return validated value weights for one named profile."""
    return ValueWeights(**load_profiles()[name]["weights"])
