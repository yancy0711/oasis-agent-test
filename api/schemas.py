"""Transport models for the OASIS HTTP API."""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, Field
from oasis.contracts.domain import TaskSpec


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
    router: Literal["ors", "haversine"] = "ors"
    candidate_upload_id: str | None = None


class RunAccepted(BaseModel):
    """Identify one queued backend run."""
    run_id: str
    status: Literal["queued"]
