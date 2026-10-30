"""Typed contracts shared by the LLM, orchestrator, tools, and outputs."""
from __future__ import annotations
from datetime import date, time
from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator, model_validator


class ValueWeights(BaseModel):
    """User-confirmed weights for registered siting indicators."""
    population_density: float = 0.4
    elderly_ratio: float = 0.2
    poverty_rate: float = 0.2
    heat_exposure: float = 0.2
    environment_burden: float = 0.0

    @field_validator("*")
    @classmethod
    def _in_range(cls, v: float) -> float:
        """Reject value weights outside the closed unit interval."""
        if not 0.0 <= v <= 1.0:
            raise ValueError("weights must be in [0,1]")
        return v

    def normalized(self) -> "ValueWeights":
        """Return a copy whose component weights sum to one."""
        total = sum(self.model_dump().values()) or 1.0
        return ValueWeights(**{k: v / total for k, v in self.model_dump().items()})


class RegionalRequirement(BaseModel):
    """Represent one user-stated minimum facility count in a named subarea."""
    region: str
    minimum_facilities: int = Field(..., ge=0)


class OperatingWindow(BaseModel):
    """One user-confirmed local service window for candidate availability."""

    date: date
    start: time
    end: time
    unknown_policy: Literal["include", "exclude"] = "include"

    @model_validator(mode="after")
    def _ordered(self) -> "OperatingWindow":
        if self.end <= self.start:
            raise ValueError("operating window end must be after start")
        return self

MobilityScenario = Literal["walking", "cycling", "driving", "walk_drive_equity"]


class RouteOriginSpec(BaseModel):
    """Bounded landmark interpretation produced by the language model."""

    entity: str = Field(min_length=1)
    context: str = ""
    positional_hint: str = ""
    raw_text: str = ""
    source: Literal["llm", "fallback"] = "llm"
    longitude: float | None = Field(None, ge=-180, le=180)
    latitude: float | None = Field(None, ge=-90, le=90)


class ResultQuery(BaseModel):
    """One read-only question over computed results, separate from the decision budget."""

    metric: Literal[
        "heat_exposure", "vulnerability", "access_disadvantage",
        "combined_disadvantage", "coverage_gap", "unsupported",
    ]
    direction: Literal["highest", "lowest"] = "highest"
    limit: int = Field(5, ge=1, le=50)
    scope: Literal["all", "vulnerable"] = "all"
    raw_text: str = ""


class Ambiguity(BaseModel):
    """A typed uncertainty that must be confirmed instead of guessed."""

    field: Literal[
        "place_names", "n_facilities", "threshold_minutes", "analysis_goal",
        "mobility_scenario", "result_queries",
    ]
    candidates: list[str] = Field(default_factory=list, max_length=10)
    question: str = Field(min_length=1)


class TaskSpec(BaseModel):
    """Structured, human-confirmable task extracted from natural language."""
    place_name: str = Field("", description="One human-confirmed geocodable US city or county")
    place_names: list[str] = Field(
        default_factory=list, max_length=5,
        description="All city or county names extracted from the request; one must be confirmed per run",
    )
    intervention: Literal["cooling_center", "mobile_clinic", "monitoring_station"] = "cooling_center"
    analysis_goal: Literal["facility_siting", "tract_review", "heat_review", "minimum_facilities",
                           "minimum_threshold", "reachability_review", "scenario_comparison",
                           "sensitivity_analysis"] = "facility_siting"
    candidate_source: Literal["osm", "csv"] = "osm"
    candidate_file: str | None = None
    allow_hypothetical_sites: bool = Field(
        False, description="Explicit opt-in post-paper extension for fixed-budget siting or fixed-threshold minimum-facility planning. Adds tract-centroid hypothetical sites; uses approximate travel, not verified venues or network coverage.",
    )
    mobility_scenario: MobilityScenario = "walking"
    recommended_mobility_scenario: MobilityScenario | None = None
    recommended_value_profile: str | None = None
    recommendation_rationale: str = ""
    operating_window: OperatingWindow | None = None
    n_facilities: int = Field(10, ge=1, le=100)
    threshold_minutes: float = Field(15.0, gt=0)
    diagnostic_fixed_inputs: list[Literal["n_facilities", "threshold_minutes"]] | None = Field(
        None, max_length=2,
        description="For coverage diagnostics, only inputs explicitly fixed by the user. A question asking how many facilities fixes threshold_minutes, not n_facilities; a minimum-time question fixes n_facilities. Include both only when both constraints were supplied. Defaults are not fixed inputs. Use [] if neither is known. Null preserves legacy single-question behavior.",
    )
    facility_capacity: float | None = Field(
        None, gt=0, description="Optional people served per selected facility"
    )
    equity_alpha: float | None = Field(
        None, ge=0.0, le=1.0,
        description="Optional human-confirmed efficiency versus access-equality trade-off",
    )
    coverage_target: float | None = Field(None, ge=0.0, le=1.0)
    coverage_group: Literal["all", "vulnerable"] = "all"
    weights: ValueWeights | None = None
    candidate_amenities: list[str] = Field(default_factory=lambda: [
        "library", "community_centre", "school", "sports_centre",
        "arts_centre", "place_of_worship",
    ])
    route_origin: RouteOriginSpec | None = Field(
        None, description="Structured point to plan from after candidate selection"
    )
    result_queries: list[ResultQuery] = Field(
        default_factory=list, max_length=8,
        description="Read-only questions answered after the primary deterministic analysis",
    )
    ambiguities: list[Ambiguity] = Field(default_factory=list, max_length=8)
    regional_requirements: list[RegionalRequirement] = Field(default_factory=list)
    sensitivity_thresholds: list[float] = Field(default_factory=list, max_length=12)
    sensitivity_facility_counts: list[int] = Field(default_factory=list, max_length=12)
    recommended_sensitivity_thresholds: list[float] = Field(default_factory=list, max_length=12)
    recommended_sensitivity_facility_counts: list[int] = Field(default_factory=list, max_length=12)
    comparison_profiles: list[str] = Field(default_factory=list, max_length=6)
    preference_question: str = ""
    notes: str = ""

    @field_validator(
        "sensitivity_thresholds", "recommended_sensitivity_thresholds"
    )
    @classmethod
    def _positive_thresholds(cls, values: list[float]) -> list[float]:
        if any(value <= 0 for value in values):
            raise ValueError("sensitivity thresholds must be positive")
        return list(dict.fromkeys(float(value) for value in values))

    @field_validator(
        "sensitivity_facility_counts", "recommended_sensitivity_facility_counts"
    )
    @classmethod
    def _positive_facility_counts(cls, values: list[int]) -> list[int]:
        if any(value < 1 or value > 100 for value in values):
            raise ValueError("sensitivity facility counts must be in [1,100]")
        return list(dict.fromkeys(int(value) for value in values))

    @model_validator(mode="after")
    def _compatible_optimization_controls(self) -> "TaskSpec":
        if self.equity_alpha is not None and self.coverage_target is not None:
            raise ValueError("equity_alpha cannot be combined with a hard coverage_target")
        if self.allow_hypothetical_sites:
            if not (self.analysis_goal == "facility_siting" or (
                    self.analysis_goal == "minimum_facilities" and self.coverage_diagnostic_handles == ("minimum_facilities",))):
                raise ValueError("Hypothetical sites support fixed-budget siting or a fixed-threshold minimum-facility question")
            if self.mobility_scenario == "walk_drive_equity" or self.operating_window or self.facility_capacity or self.regional_requirements or self.equity_alpha is not None or self.result_queries:
                raise ValueError("Hypothetical sites currently support a single travel mode without capacity, operating-window, regional, Pareto-alpha, or extra ranking constraints")
        return self

    def is_complete(self) -> bool:
        """Return whether all human value judgments have been supplied."""
        return self.analysis_goal != "facility_siting" or self.weights is not None

    @property
    def coverage_diagnostic_handles(self) -> tuple[str, ...]:
        fixed = self.diagnostic_fixed_inputs
        if fixed is None:
            fixed = ["threshold_minutes"] if self.analysis_goal == "minimum_facilities" else ["n_facilities"]
        handles = []
        if "threshold_minutes" in fixed:
            handles.append("minimum_facilities")
        if "n_facilities" in fixed:
            handles.append("minimum_threshold")
        return tuple(handles)


RoutingMode = Literal["quick", "precise", "auto"]
RunStatus = Literal[
    "queued", "running", "cancelling", "cancelled", "superseded",
    "interrupted", "succeeded", "awaiting_hitl", "failed",
]


class RunSpec(BaseModel):
    """Bind one decision task to an explicit, reproducible execution mode."""
    task: TaskSpec
    mode: RoutingMode = "auto"


class ExecutionProvenance(BaseModel):
    """Describe how one quick or precise execution phase was computed."""
    mode: RoutingMode
    phase: Literal["quick", "precise"]
    router_requested: Literal["haversine", "ors"]
    router_used: str | None = None
    accuracy: Literal["approximate", "network"] | None = None
    status: Literal["complete", "failed", "cancelled"] = "complete"
    parameters: dict[str, Any] = Field(default_factory=dict)
    cache_stats: dict[str, Any] = Field(default_factory=dict)


class Provenance(BaseModel):
    """Describe the source and processing tier of one dataset."""
    source: str
    tier: Literal["live", "cache", "sample"] = "live"
    record_count: int = 0
    crs: str = "EPSG:4326"
    notes: str = ""


class ToolError(BaseModel):
    """Return an actionable tool failure without exposing raw exceptions."""
    kind: Literal["ambiguous", "rate_limit", "not_found", "schema_mismatch", "crs_error",
                  "infeasible", "timeout", "insufficient_candidates", "constraint_conflict", "unknown"]
    message: str
    hint: str = ""
    retryable: bool = True


class ToolResult(BaseModel):
    """Return a lightweight result while large objects remain in Context."""
    ok: bool
    tool: str
    handle: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    provenance: Provenance | None = None
    error: ToolError | None = None


class SiteSolution(BaseModel):
    """Store one optimization solution and its reported performance."""
    profile_name: str
    selected_ids: list[str]
    covered_population: float
    covered_ratio: float
    vulnerable_covered_ratio: float
    population_weighted_access_gini: float | None = None
    mean_access_score: float = 0.0
    gradual_coverage_ratio: float = 0.0
    assigned_population: float = 0.0
    facility_loads: dict[str, float] = Field(default_factory=dict)
    facility_capacities: dict[str, float] = Field(default_factory=dict)
    maximum_capacity_utilization: float | None = None
    mean_travel_minutes: float
    weights_used: ValueWeights | None
    solver_status: str


class ParetoPoint(BaseModel):
    """Store one efficiency-equity trade-off solution."""
    alpha: float
    efficiency_metric: float
    equity_metric: float
    population_weighted_gini: float | None = None
    solution: SiteSolution


class PlanStep(BaseModel):
    """Describe one registered tool call proposed by the planner."""
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""


class AgentPlan(BaseModel):
    """Store an ordered plan for fixed-baseline evaluation only."""
    steps: list[PlanStep]
    reasoning: str = ""


class AgentAction(BaseModel):
    """Describe one observable action selected from the current state."""
    kind: Literal["tool", "finish", "hitl"]
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    message: str = ""
    suggested_changes: dict[str, Any] = Field(default_factory=dict)


class AgentEvent(BaseModel):
    """Record an auditable action or observation, never hidden reasoning."""
    stage: Literal["parse", "hitl", "plan", "decision", "tool_call", "tool_result", "observation", "repair", "explanation"]
    message: str
    tool: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ResultNarrative(BaseModel):
    """Store evidence-grounded decision interpretation and self-reflection."""
    decision_summary: str
    self_reflection: str

class RecoveryProposal(BaseModel):
    """Describe a user-confirmable recovery without silently changing constraints."""
    diagnosis: str
    question: str
    suggested_changes: dict[str, Any] = Field(default_factory=dict)
    alternatives: list[dict[str, Any]] = Field(default_factory=list)
