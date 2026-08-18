"""FastAPI routes for the OASIS parsing, HITL, run, and artifact services."""
from __future__ import annotations
from fastapi import APIRouter, FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse

from api.schemas import (ParseRequest, ParseResponse, PlaceConfirmRequest,
                         PlaceConfirmResponse, PlaceRequest, PlaceResolveResponse,
                         RunAccepted, RunRequest, RunRevisionRequest,
                         RunSnapshotResponse, UiConfigResponse, UploadResponse,
                         PlaceRouteRequest, PointRouteRequest, PointRouteResponse,
                         DataSourceStatusResponse)
from api.schemas import OfficialResourcesResponse
from api.schemas import (LlmConnectionRequest, LlmConnectionResponse,
                         LlmModelsRequest, LlmModelsResponse, LlmSessionRevokeResponse)
from api.schemas import StreetViewResponse
from oasis.core.data.streetview import nearest_streetview
from oasis.services.application import OasisService


def create_app(service: OasisService | None = None) -> FastAPI:
    """Create an API application with an injectable service for offline tests."""
    app = FastAPI(title="OASIS Agent API", version="1.0.0")
    app.state.service = service or OasisService()
    router = APIRouter(prefix="/v1")

    @app.get("/health")
    def health() -> dict:
        """Report process health without invoking external providers."""
        return {"status": "ok"}

    @router.get("/config", response_model=UiConfigResponse)
    def ui_config() -> dict:
        """Return non-secret defaults and registered value profiles."""
        cfg = app.state.service.cfg
        return {"optimization": cfg.get("optimization", {}),
                "accessibility": {key: value for key, value in cfg.get("accessibility", {}).items()
                                  if key not in {"ors_base_url"}},
                "agent": {"max_steps": cfg.get("llm", {}).get("max_agent_steps", 14),
                          "provider": cfg.get("llm", {}).get("provider", "openrouter"),
                          "model": cfg.get("llm", {}).get("model", ""),
                          "base_url": cfg.get("llm", {}).get("base_url", "")},
                "execution": {
                    "modes": ["auto", "quick", "precise"],
                    "default_mode": cfg.get("execution", {}).get("default_mode", "auto"),
                },
                "value_profiles": app.state.service.profiles}

    @router.get("/data-sources/status", response_model=DataSourceStatusResponse)
    def data_source_status(refresh: bool = False) -> dict:
        """Check approved providers and versions without replacing configured data."""
        return app.state.service.data_source_status(force=refresh)

    @router.get("/official-resources", response_model=OfficialResourcesResponse)
    def official_resources(place_name: str) -> dict:
        """Discover city/county official cooling-resource catalogs without replacing run data."""
        return app.state.service.official_resources(place_name)

    @app.get("/streetview", response_model=StreetViewResponse)
    def streetview(lat: float, lon: float, radius_m: int | None = None) -> dict:
        """Return the nearest Mapillary image while keeping its access token server-side."""
        configured_radius = app.state.service.cfg.get("data", {}).get("streetview", {}).get("radius_m", 100)
        try:
            return nearest_streetview(lat, lon, int(radius_m or configured_radius))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/llm/test", response_model=LlmConnectionResponse)
    def test_llm_connection(request: Request, settings: LlmConnectionRequest) -> dict:
        """Test one BYOK connection and return only an opaque in-memory session ID."""
        host = request.client.host if request.client else ""
        allow_private = host in {"127.0.0.1", "::1", "localhost", "testclient"}
        try:
            return app.state.service.test_llm_connection(
                settings.provider, settings.base_url, settings.model,
                settings.api_key.get_secret_value(), allow_private=allow_private,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    @router.post("/llm/models", response_model=LlmModelsResponse)
    def list_llm_models(request: Request, settings: LlmModelsRequest) -> dict:
        """Discover endpoint models; credentials are used for this request only."""
        host = request.client.host if request.client else ""
        allow_private = host in {"127.0.0.1", "::1", "localhost", "testclient"}
        try:
            return app.state.service.list_llm_models(
                settings.provider, settings.base_url, settings.api_key.get_secret_value(),
                allow_private=allow_private,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    @router.delete("/llm/sessions/{session_id}", response_model=LlmSessionRevokeResponse)
    def revoke_llm_session(session_id: str) -> dict:
        """Clear a visitor-supplied credential when its browser session ends."""
        return {"revoked": app.state.service.revoke_llm_session(session_id)}

    @router.post("/parse", response_model=ParseResponse)
    def parse(request: ParseRequest) -> dict:
        """Parse one natural-language task draft."""
        try: return app.state.service.parse(request.prompt, request.llm_session_id)
        except KeyError as exc: raise HTTPException(401, str(exc)) from exc
        except Exception as exc: raise HTTPException(422, str(exc)) from exc

    @router.post("/places/resolve", response_model=PlaceResolveResponse)
    def resolve_place(request: PlaceRequest) -> dict:
        """Resolve one US place into compact candidate metadata."""
        try: return app.state.service.resolve_place(request.place_name)
        except Exception as exc: raise HTTPException(422, str(exc)) from exc

    @router.post("/places/confirm", response_model=PlaceConfirmResponse)
    def confirm_place(request: PlaceConfirmRequest) -> dict:
        """Confirm one candidate from a prior resolution."""
        try: return app.state.service.confirm_place(request.resolution_id, request.candidate_id)
        except KeyError as exc: raise HTTPException(404, str(exc)) from exc

    @router.post("/uploads", response_model=UploadResponse)
    async def upload_candidates(file: UploadFile = File(...)) -> dict:
        """Upload one candidate CSV into backend-managed storage."""
        try: return app.state.service.upload_candidates(file.filename or "candidates.csv", await file.read())
        except ValueError as exc: raise HTTPException(422, str(exc)) from exc

    @router.post("/runs", response_model=RunAccepted, response_model_exclude_none=True,
                 status_code=status.HTTP_202_ACCEPTED)
    def start_run(request: RunRequest) -> dict:
        """Queue one confirmed task for bounded background execution."""
        try: return app.state.service.start_run(
            request.task, request.mode, request.candidate_upload_id,
            llm_session_id=request.llm_session_id,
        )
        except KeyError as exc: raise HTTPException(404, str(exc)) from exc

    @router.get("/runs/{run_id}", response_model=RunSnapshotResponse)
    def get_run(run_id: str) -> dict:
        """Return current status, progress, events, and any terminal result."""
        try: return app.state.service.run_snapshot(run_id)
        except KeyError as exc: raise HTTPException(404, str(exc)) from exc

    @router.post("/runs/{run_id}/cancel", response_model=RunSnapshotResponse,
                 status_code=status.HTTP_202_ACCEPTED)
    def cancel_run(run_id: str) -> dict:
        """Terminate the isolated run process while retaining committed caches."""
        try: return app.state.service.cancel_run(run_id)
        except KeyError as exc: raise HTTPException(404, str(exc)) from exc

    @router.post("/runs/{run_id}/revisions", response_model=RunAccepted,
                 response_model_exclude_none=True, status_code=status.HTTP_202_ACCEPTED)
    def revise_run(run_id: str, request: RunRevisionRequest) -> dict:
        """Create an immutable child run from revised confirmed parameters."""
        try:
            return app.state.service.revise_run(
                run_id, request.task, request.mode, request.candidate_upload_id,
                llm_session_id=request.llm_session_id,
            )
        except KeyError as exc: raise HTTPException(404, str(exc)) from exc
        except ValueError as exc: raise HTTPException(422, str(exc)) from exc

    @router.post("/runs/{run_id}/routes", response_model=PointRouteResponse)
    def plan_point_route(run_id: str, request: PointRouteRequest) -> dict:
        """Plan a route from a user-selected point to the network-nearest selected candidate facility."""
        try:
            return app.state.service.plan_point_route(
                run_id, request.longitude, request.latitude, request.mode
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    @router.post("/runs/{run_id}/routes/from-place", response_model=PointRouteResponse)
    def plan_place_route(run_id: str, request: PlaceRouteRequest) -> dict:
        """Geocode an origin within the study area, then plan to the network-nearest site."""
        try:
            return app.state.service.plan_place_route(run_id, request.query, request.mode)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc

    @router.get("/runs/{run_id}/artifacts/{name}")
    def get_artifact(run_id: str, name: str) -> FileResponse:
        """Serve one allow-listed artifact; caller paths are never accepted."""
        try: path = app.state.service.artifact(run_id, name)
        except KeyError as exc: raise HTTPException(404, str(exc)) from exc
        media = {".geojson": "application/geo+json", ".png": "image/png", ".md": "text/markdown"}.get(path.suffix.lower())
        return FileResponse(
            path, media_type=media, filename=path.name,
            content_disposition_type="inline",
        )

    app.include_router(router)
    return app


app = create_app()
