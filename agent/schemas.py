"""Single source of truth for all cross-layer data contracts.

Rule: the LLM may only produce/consume structures defined here -- never raw
DataFrames or untyped dicts. This keeps the LLM/tool boundary verifiable.
"""
from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator


# ---------- 1. Task spec: where natural language lands ----------
class ValueWeights(BaseModel):
    population_density: float = 0.4
    elderly_ratio: float = 0.2
    poverty_rate: float = 0.2
    heat_exposure: float = 0.2

    @field_validator("*")
    @classmethod
    def _in_range(cls, v: float) -> float:
        """Reject value weights outside the closed unit interval."""
        assert 0.0 <= v <= 1.0, "weights must be in [0,1]"
        return v

    def normalized(self) -> "ValueWeights":
        """Return a copy whose component weights sum to one."""
        total = sum(self.model_dump().values()) or 1.0
        return ValueWeights(**{k: v / total for k, v in self.model_dump().items()})


class TaskSpec(BaseModel):
    """Structured task extracted by the LLM. Missing values -> HITL clarification.

    weights=None is meaningful: it signals a value judgment gap that must be
    resolved by a human, never silently filled by the agent.
    """
    place_name: str = Field(..., description="Geocodable place, e.g. 'Los Angeles, California, USA'")
    intervention: Literal["cooling_center", "mobile_clinic", "monitoring_station"] = "cooling_center"
    n_facilities: int = Field(8, ge=1, le=100)
    threshold_minutes: float = Field(15.0, gt=0)
    weights: ValueWeights | None = None
    candidate_amenities: list[str] = Field(default_factory=lambda: ["library", "community_centre", "school"])
    notes: str = ""  # assumptions/ambiguities recorded by the LLM; goes into the report

    def is_complete(self) -> bool:
        """Return whether all human value judgments have been supplied."""
        return self.weights is not None


# ---------- 2. Tool contract: uniform envelope for every tool ----------
class Provenance(BaseModel):
    """Where each dataset came from and how fresh it is. Reported transparently."""
    source: str
    tier: Literal["live", "cache", "sample"] = "live"
    record_count: int = 0
    crs: str = "EPSG:4326"
    notes: str = ""


class ToolError(BaseModel):
    """Structured error. Tools never raise raw exceptions to the agent --
    a raw traceback gives the LLM nothing actionable to repair."""
    kind: Literal["ambiguous", "rate_limit", "not_found", "schema_mismatch",
                  "crs_error", "infeasible", "timeout", "unknown"]
    message: str
    hint: str = ""       # repair suggestion for the LLM
    retryable: bool = True


class ToolResult(BaseModel):
    """Uniform tool return. Large objects stay in Context under `handle`;
    the LLM only ever sees the lightweight `summary`."""
    ok: bool
    tool: str
    handle: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    provenance: Provenance | None = None
    error: ToolError | None = None


# ---------- 3. Optimization results ----------
class SiteSolution(BaseModel):
    profile_name: str
    selected_ids: list[str]
    covered_population: float
    covered_ratio: float
    vulnerable_covered_ratio: float   # core equity metric
    mean_travel_minutes: float
    weights_used: ValueWeights | None
    solver_status: str


class ParetoPoint(BaseModel):
    alpha: float                      # 0 = efficiency end, 1 = equity end
    efficiency_metric: float
    equity_metric: float
    solution: SiteSolution


# ---------- 4. Agent plan ----------
class PlanStep(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""               # one-liner; feeds the methods section of the report


class AgentPlan(BaseModel):
    steps: list[PlanStep]
    reasoning: str = ""
