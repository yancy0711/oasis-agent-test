"""Resident discovery contracts, deliberately separate from planning TaskSpec."""
from typing import Literal
from pydantic import BaseModel, Field, model_validator

ResidentCategory = Literal["library", "community_centre", "swimming_pool", "mall",
                           "sports_centre", "arts_centre", "place_of_worship", "park"]
ResidentMode = Literal["walking", "cycling", "driving"]
DEFAULT_CATEGORIES = ["library", "community_centre", "swimming_pool", "mall",
                      "sports_centre", "arts_centre", "place_of_worship", "park"]


class ResidentPreferences(BaseModel):
    origin_query: str | None = Field(None, description="Named starting place, including US city abbreviations expanded in context. Null only when no named origin is supplied.")
    use_current_location: bool = Field(False, description="Use browser location only when no named origin is supplied or the user explicitly requests current location.")
    mode: ResidentMode = "walking"
    threshold_minutes: float = Field(15, gt=0, le=120)
    categories: list[ResidentCategory] = Field(default_factory=lambda: DEFAULT_CATEGORIES.copy())
    excluded_categories: list[ResidentCategory] = Field(default_factory=list)
    preferred_categories: list[ResidentCategory] = Field(default_factory=list)
    indoor_only: bool = False
    clarification: str = ""
    needs_clarification: bool = False
    notice: str = ""


class ResidentParseRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    previous: ResidentPreferences | None = None
    llm_session_id: str | None = None


class ResidentCoordinate(BaseModel):
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)


class ResidentCoordinateRequest(ResidentCoordinate):
    request_id: str | None = Field(None, min_length=8, max_length=64, pattern=r"^[a-zA-Z0-9-]+$")


class ResidentOrigin(ResidentCoordinate):
    label: str
    country_code: str = ""


class ResidentOriginResponse(BaseModel):
    candidates: list[ResidentOrigin]
    message: str


class ResidentOriginQuery(BaseModel):
    request_id: str | None = Field(None, min_length=8, max_length=64, pattern=r"^[a-zA-Z0-9-]+$")
    query: str = Field(min_length=2, max_length=300)


class ResidentSearchRequest(ResidentCoordinate):
    request_id: str = Field(min_length=8, max_length=64, pattern=r"^[a-zA-Z0-9-]+$")
    preferences: ResidentPreferences
    radius_km: float = Field(5, ge=1, le=30)


class ResidentPlace(ResidentCoordinate):
    id: str
    name: str
    category: ResidentCategory
    duration_minutes: float
    opening_hours: str = "Unknown"
    access: str = "Unknown"
    fee: str = "Unknown"
    indoor: str = "Unknown"
    coordinate_source: str
    osm_url: str


class ResidentSearchResponse(BaseModel):
    search_id: str
    origin: ResidentOrigin
    mode: ResidentMode
    threshold_minutes: float
    radius_km: float
    candidates: list[ResidentPlace]
    complete: bool
    candidate_limit: int = 50
    discovered_count: int
    checked_count: int
    message: str
    source: str = "OpenStreetMap candidates; ORS network travel times"


class ResidentRouteRequest(BaseModel):
    request_id: str | None = Field(None, min_length=8, max_length=64, pattern=r"^[a-zA-Z0-9-]+$")
    search_id: str
    candidate_id: str


class InterpretRequest(ResidentParseRequest):
    audience: Literal["research", "personal"] = "research"


class RequestInterpretation(BaseModel):
    action: Literal["select_facilities", "find_destination", "assess_area", "compare_scenarios", "clarify"]
    workflow: Literal["research", "personal", "clarify"]
    reason: str
    preferences: ResidentPreferences | None = None

    @model_validator(mode="after")
    def require_resident_preferences(self):
        expected = ("personal" if self.action == "find_destination" else
                    "clarify" if self.action == "clarify" else "research")
        if self.workflow != expected:
            self.workflow = expected
            self.reason = {
                "personal": "Finding a destination uses nearby search.",
                "research": "Facility selection, assessment and comparison use research and planning.",
                "clarify": "Please clarify which task you want to perform.",
            }[expected]
        if self.workflow != "personal":
            self.preferences = None
        if self.workflow == "personal" and self.preferences is None:
            raise ValueError("A nearby-place request must include its parsed preferences.")
        return self
