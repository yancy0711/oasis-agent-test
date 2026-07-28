"""Runtime settings and value-profile loading shared by application layers."""
from __future__ import annotations
import os
from pathlib import Path
import yaml
from dotenv import load_dotenv
from oasis.contracts.domain import ValueWeights

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILES_PATH = PROJECT_ROOT / "config" / "value_profiles.yaml"


def load_cfg() -> dict:
    """Load project settings, local environment variables, and portable paths."""
    load_dotenv(PROJECT_ROOT / ".env")
    path = Path(os.getenv("OASIS_CONFIG_PATH", PROJECT_ROOT / "config" / "settings.yaml"))
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    output = Path(os.getenv("OASIS_OUTPUT_DIR", cfg.get("output", {}).get("dir", "outputs_run")))
    cfg["output"] = {**cfg.get("output", {}), "dir": str(output if output.is_absolute() else PROJECT_ROOT / output)}
    return cfg


def load_profiles(path: Path = PROFILES_PATH) -> dict[str, dict]:
    """Load named profiles and reject weights that do not sum to one."""
    profiles = yaml.safe_load(path.read_text(encoding="utf-8"))["profiles"]
    for name, profile in profiles.items():
        total = sum(profile["weights"].values())
        if abs(total - 1.0) > 1e-6: raise ValueError(f"profile '{name}' weights sum to {total}, expected 1.0")
    return profiles


def get_weights(name: str) -> ValueWeights:
    """Return validated value weights for one named profile."""
    return ValueWeights(**load_profiles()[name]["weights"])
