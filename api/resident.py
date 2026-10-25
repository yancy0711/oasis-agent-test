"""Request intent and resident APIs; nearby search has no solver dependency."""
import json
from pathlib import Path
import requests
import httpx
from fastapi import APIRouter, HTTPException, Request
from api.cancellation import call_while_connected
from api.schemas import PointRouteResponse
from oasis.agent.orchestrator import LLMClient, load_prompt
from oasis.contracts.resident import (ResidentCoordinateRequest, ResidentOrigin, ResidentOriginQuery,
    ResidentOriginResponse, ResidentParseRequest, ResidentPreferences, ResidentRouteRequest,
    ResidentSearchRequest, ResidentSearchResponse, InterpretRequest, RequestInterpretation)
from oasis.core.cancellation import RunCancelled
from oasis.core.retry import TransientError
from oasis.services.resident import ResidentService
from oasis.settings import load_resident_categories


def create_resident_router(service):
    router = APIRouter(prefix="/v1", tags=["resident"])
    nearby = ResidentService(service.cfg)

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except RunCancelled as exc:
            raise HTTPException(409, "Nearby search cancelled.") from exc
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except (RuntimeError, requests.RequestException, httpx.HTTPError, TimeoutError, TransientError) as exc:
            raise HTTPException(503, str(exc)) from exc

    def interpret(request, schema, prompt_name):
        session = service._llm_session_config(request.llm_session_id)
        llm = LLMClient(session) if session else service.llm_factory()
        if isinstance(llm, LLMClient):
            llm.client = llm.client.with_options(timeout=12, max_retries=0)
        root = Path(__file__).resolve().parents[1] / "oasis/agent/prompts"
        prompt = load_prompt(Path(prompt_name).stem)
        if prompt_name == "request_intent.md":
            prompt += "\nResident preference extraction rules:\n" + (root / "resident_parsing.md").read_text(encoding="utf-8")
        prompt += "\nSupported place-category catalog (semantic hints, not verified site amenities):\n" + json.dumps(load_resident_categories())
        data = llm.structured(prompt, json.dumps({
            "previous": request.previous.model_dump() if request.previous else None,
            "current_tab": getattr(request, "audience", "personal"),
            "request": request.prompt}), schema=schema.model_json_schema())
        parsed = schema.model_validate(data)
        preferences = parsed if isinstance(parsed, ResidentPreferences) else parsed.preferences
        if preferences is not None:
            # Indoor/outdoor intent selects categories; the removed strict-tag
            # control must not survive as a hidden filter from earlier context.
            preferences.indoor_only = False
        return parsed

    @router.post("/requests/interpret", response_model=RequestInterpretation)
    def interpret_request(request: InterpretRequest):
        return call(interpret, request, RequestInterpretation, "request_intent.md")

    @router.post("/resident/parse", response_model=ResidentPreferences)
    def parse(request: ResidentParseRequest):
        return call(interpret, request, ResidentPreferences, "resident_parsing.md")

    @router.post("/resident/origin", response_model=ResidentOrigin)
    async def locate(request: ResidentCoordinateRequest, connection: Request):
        return await call_while_connected(connection, call, nearby.locate, request)

    @router.post("/resident/origins", response_model=ResidentOriginResponse)
    async def origins(request: ResidentOriginQuery, connection: Request):
        candidates = await call_while_connected(connection, call, nearby.origins, request)
        return {"candidates": candidates, "message": "Confirm your starting point. City names use a representative point; enter an address for precise navigation."}

    @router.post("/resident/search", response_model=ResidentSearchResponse)
    async def search(request: ResidentSearchRequest, connection: Request):
        return await call_while_connected(connection, call, nearby.search, request)

    @router.post("/resident/cancel/{request_id}")
    def cancel(request_id: str):
        if len(request_id) > 64:
            raise HTTPException(422, "Invalid request ID")
        if not nearby.cancel(request_id):
            raise HTTPException(503, "The request is still stopping. Retry Stop and reset before searching again.")
        return {"cancelled": True}

    @router.post("/resident/routes", response_model=PointRouteResponse)
    async def route(request: ResidentRouteRequest, connection: Request):
        return await call_while_connected(connection, call, nearby.route, request.search_id, request.candidate_id, request.request_id)

    return router
