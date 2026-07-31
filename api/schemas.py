"""Transport models for the OASIS HTTP API."""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field, model_validator
from oasis.contracts.domain import RoutingMode, TaskSpec


class ParseRequest(BaseModel):
    """Carry one natural-language request to the backend parser."""
    prompt: str = Field(min_length=1)


class PlaceRequest(BaseModel):
    """Carry one US place query to deterministic resolution."""
    place_name: str = Field(min_length=1)


class PlaceConfirmRequest(BaseModel):
    """Select one opaque candidate from a prior resolution."""
    resolution_id: str
    candidate_id: str


class RunRequest(BaseModel):
    """Start one confirmed agent run."""
    task: TaskSpec
    mode: RoutingMode = "auto"
    router: Literal["ors", "haversine"] | None = None
    candidate_upload_id: str | None = None

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


class RunAccepted(BaseModel):
    """Identify one queued backend run."""
    run_id: str
    status: Literal["queued"]
    mode: RoutingMode | None = None
    parent_run_id: str | None = None
    revision: int | None = None
    reuse: list[str] | None = None
    recompute: list[str] | None = None
