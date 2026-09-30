"""Transport models for the OASIS HTTP API."""
from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from oasis.contracts.domain import (ExecutionProvenance, RecoveryProposal,
                                    ResultNarrative, RoutingMode, RunStatus,
                                    TaskSpec)


class ParseRequest(BaseModel):
    """Carry one natural-language request to the backend parser."""
    prompt: str = Field(min_length=1)
    llm_session_id: str | None = None


class LlmConnectionRequest(BaseModel):
    """Test and retain one visitor-supplied model credential in memory only."""

    provider: Literal["gemini", "openai", "openrouter", "custom", "local"]
    base_url: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    api_key: SecretStr = Field(default=SecretStr(""), max_length=1000)


class LlmConnectionResponse(BaseModel):
    """Return an opaque short-lived model session without echoing its secret."""

    session_id: str
    provider: str
    model: str
    resolved_model: str
    latency_ms: int
    expires_in_seconds: int
    structured_output: bool
    compatibility_attempts: int = 1


class LlmModelsRequest(BaseModel):
    """Request model discovery from one user-configured endpoint."""

    provider: Literal["gemini", "openai", "openrouter", "custom", "local"]
    base_url: str = Field(min_length=1, max_length=500)
    api_key: SecretStr = Field(default=SecretStr(""), max_length=1000)


class LlmModelsResponse(BaseModel):
    """Return model IDs exposed by the endpoint without retaining credentials."""

    provider: str
    models: list[str]


class LlmSessionRevokeResponse(BaseModel):
    """Confirm that a browser-scoped model session is no longer retained."""

    revoked: bool


class StreetViewResponse(BaseModel):
    """Return one nearby Mapillary image or an explicit graceful fallback."""

    available: bool
    status: Literal["available", "no_image", "not_configured", "error"]
    provider: str = "Mapillary"
    message: str
    image_id: str | None = None
    image_url: str | None = None
    viewer_url: str | None = None
    captured_at: str | int | None = None
    creator: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    distance_m: float | None = None


class RouteOriginCandidate(BaseModel):
    """Expose one ranked point or administrative interpretation of a route origin."""

    candidate_id: str
    display_name: str
    place_name: str
    place_type: str
    entity: str
    context: str = ""
    positional_hint: str = ""
    administrative: bool = False
    longitude: float | None = None
    latitude: float | None = None


class CoordinateOriginRequest(BaseModel):
    """Carry one browser geolocation coordinate for reverse resolution."""

    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)


class NamedOriginRequest(BaseModel):
    """Carry a user-entered route origin without reparsing the whole task."""

    query: str = Field(min_length=2, max_length=300)


class CoordinateOriginResponse(BaseModel):
    """Return supported US parent boundaries for one exact route origin."""

    outside_us: bool
    message: str
    candidates: list[RouteOriginCandidate] = Field(default_factory=list)


class ParseResponse(BaseModel):
    """Return one proposed task draft and its observable parse events."""

    disposition: Literal["full_analysis", "quick_diagnostic", "needs_clarification", "out_of_scope"]
    task: TaskSpec | None = None
    missing_place: bool
    message: str
    reason: str = ""
    alternative: str = ""
    events: list[dict[str, Any]] = Field(default_factory=list)
    route_origin_candidates: list[RouteOriginCandidate] = Field(default_factory=list)
    place_name_options: list[str] = Field(default_factory=list)


class PlaceRequest(BaseModel):
    """Carry one US place query to deterministic resolution."""
    place_name: str = Field(min_length=1)


class PlaceConfirmRequest(BaseModel):
    """Select one opaque candidate from a prior resolution."""
    resolution_id: str
    candidate_id: str


class PlaceCandidate(BaseModel):
    """Expose compact place metadata without transferring boundary geometry."""

    candidate_id: str
    canonical_name: str
    display_name: str
    place_type: str
    label: str


class PlaceResolveResponse(BaseModel):
    """Return an opaque place-resolution session and retained candidates."""

    resolution_id: str
    query: str
    ambiguous: bool
    candidates: list[PlaceCandidate]
    selected: PlaceCandidate | None = None


class PlaceConfirmResponse(PlaceCandidate):
    """Return the canonical place selected by the user."""

    query: str


class UploadResponse(BaseModel):
    """Identify one backend-managed candidate upload."""

    upload_id: str
    filename: str
    size: int


class RunRequest(BaseModel):
    """Start one confirmed agent run."""
    task: TaskSpec
    mode: RoutingMode = "auto"
    router: Literal["ors", "haversine"] | None = None
    candidate_upload_id: str | None = None
    llm_session_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _legacy_router(cls, value):
        """Map the old router field without allowing contradictory requests."""
        data = dict(value)
        router = data.get("router")
        mapped = {"ors": "precise", "haversine": "quick"}.get(router)
        if mapped and "mode" not in data:
            data["mode"] = mapped
        elif mapped and data.get("mode") != mapped:
            raise ValueError("router and mode describe different routing policies")
        return data


class RunRevisionRequest(BaseModel):
    """Create an immutable child run, optionally changing task or mode."""
    task: TaskSpec | None = None
    mode: RoutingMode | None = None
    router: Literal["ors", "haversine"] | None = None
    candidate_upload_id: str | None = None
    llm_session_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _legacy_router(cls, value):
        data = dict(value)
        router = data.get("router")
        mapped = {"ors": "precise", "haversine": "quick"}.get(router)
        if mapped and data.get("mode") is None:
            data["mode"] = mapped
        elif mapped and data.get("mode") != mapped:
            raise ValueError("router and mode describe different routing policies")
        return data


class PointRouteRequest(BaseModel):
    """Plan one transient route from a user-selected exact coordinate."""

    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)
    mode: Literal["walking", "cycling", "driving"] = "walking"


class PointRouteResponse(BaseModel):
    """Return a GeoJSON route and compact route/environment summary."""

    type: Literal["FeatureCollection"]
    features: list[dict[str, Any]]
    summary: dict[str, Any]


class PlaceRouteRequest(BaseModel):
    """Plan one transient route from a geocoded address or named point."""

    query: str = Field(min_length=1, max_length=300)
    mode: Literal["walking", "cycling", "driving"] = "walking"


class RunAccepted(BaseModel):
    """Identify one queued backend run."""
    run_id: str
    status: Literal["queued"]
    mode: RoutingMode | None = None
    parent_run_id: str | None = None
    revision: int | None = None
    reuse: list[str] | None = None
    recompute: list[str] | None = None


class UiExecutionConfig(BaseModel):
    """Describe execution modes available to the web presentation."""

    modes: list[RoutingMode]
    default_mode: RoutingMode


class UiConfigResponse(BaseModel):
    """Return presentation defaults, profiles, and the public basemap credential."""

    optimization: dict[str, Any]
    accessibility: dict[str, Any]
    agent: dict[str, Any]
    execution: UiExecutionConfig
    value_profiles: dict[str, Any]
    carto_basemap_api_key: str = ""


class DataSourceStatusResponse(BaseModel):
    """Expose notify-only provider checks without changing configured data."""

    checked_at: str
    policy: Literal["notify_only_no_automatic_replacement"]
    updates_available: bool
    sources: list[dict[str, Any]]
    compatibility_warnings: list[str] = Field(default_factory=list)


class OfficialResourcesResponse(BaseModel):
    """Expose official catalog matches without asserting facility-level verification."""

    checked_at: str
    place_name: str
    place_type: Literal["city", "county", "state"]
    status: Literal["found", "not_found", "not_applicable", "unverified"]
    resources: list[dict[str, str]] = Field(default_factory=list)
    message: str


class RunProgress(BaseModel):
    """Describe the latest transient progress checkpoint."""

    model_config = ConfigDict(extra="allow")

    kind: str = "progress"
    label: str
    done: int
    total: int
    unit: str = "items"
    fraction: float
    eta_seconds: float | None = None
    cache_hits: int = 0
    remote_pairs: int = 0
    inflight_pairs: int = 0
    stage_started_at: str | None = None
    activity_at: str | None = None
    heartbeat_at: str | None = None
    stage_elapsed_seconds: float = 0.0
    last_activity_seconds: float = 0.0


class PublicAgentEvent(BaseModel):
    """Expose one ordered, observable agent event."""

    seq: int | None = None
    created_at: str | None = None
    kind: str = "agent"
    stage: str
    message: str
    tool: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class RunResultResponse(BaseModel):
    """Expose stable run outcome fields while keeping diagnostics extensible."""

    ok: bool
    paused: bool = False
    message: str = ""
    failed_tool: str | None = None
    results: list[dict[str, Any]] = Field(default_factory=list)
    lineage: list[dict[str, Any]] = Field(default_factory=list)
    outputs: dict[str, Any] | None = None
    heat_summary: dict[str, Any] | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    narrative: ResultNarrative | None = None
    recovery: RecoveryProposal | None = None
    history: list[dict[str, Any]] = Field(default_factory=list)
    task: TaskSpec
    metrics: dict[str, float | None] = Field(default_factory=dict)
    pareto_points: list[dict[str, Any]] = Field(default_factory=list)
    routing_status: dict[str, Any] | None = None
    execution: ExecutionProvenance
    artifacts: dict[str, str] = Field(default_factory=dict)


class RunSnapshotResponse(BaseModel):
    """Expose durable run state while retaining flexible result payloads."""

    run_id: str
    parent_run_id: str | None = None
    revision: int
    status: RunStatus
    phase: str | None = None
    mode: RoutingMode
    spec: dict[str, Any]
    task: TaskSpec
    events: list[PublicAgentEvent] = Field(default_factory=list)
    progress: RunProgress | None = None
    preview_result: RunResultResponse | None = None
    result: RunResultResponse | None = None
    error: str | None = None
    cancel_reason: str | None = None
    created_at: str
    updated_at: str
