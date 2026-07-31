"""FastAPI routes for the OASIS parsing, HITL, run, and artifact services."""
from __future__ import annotations
from fastapi import FastAPI, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse

from api.schemas import (ParseRequest, PlaceConfirmRequest, PlaceRequest,
                         RunAccepted, RunRequest, RunRevisionRequest)
from oasis.services.application import OasisService


def create_app(service: OasisService | None = None) -> FastAPI:
    """Create an API application with an injectable service for offline tests."""
    app = FastAPI(title="OASIS Agent API", version="1.0.0")
    app.state.service = service or OasisService()

    @app.get("/health")
    def health() -> dict:
        """Report process health without invoking external providers."""
        return {"status": "ok"}

    @app.get("/v1/config")
    def ui_config() -> dict:
        """Return non-secret defaults and registered value profiles."""
        cfg = app.state.service.cfg
        return {"optimization": cfg.get("optimization", {}),
                "accessibility": {key: value for key, value in cfg.get("accessibility", {}).items()
                                  if key not in {"ors_base_url"}},
                "agent": {"max_steps": cfg.get("llm", {}).get("max_agent_steps", 14)},
                "execution": {
                    "modes": ["auto", "quick", "precise"],
                    "default_mode": cfg.get("execution", {}).get("default_mode", "auto"),
                },
                "value_profiles": app.state.service.profiles}

    @app.post("/v1/parse")
    def parse(request: ParseRequest) -> dict:
        """Parse one natural-language task draft."""
        try: return app.state.service.parse(request.prompt)
        except Exception as exc: raise HTTPException(422, str(exc)) from exc

    @app.post("/v1/places/resolve")
    def resolve_place(request: PlaceRequest) -> dict:
        """Resolve one US place into compact candidate metadata."""
        try: return app.state.service.resolve_place(request.place_name)
        except Exception as exc: raise HTTPException(422, str(exc)) from exc

    @app.post("/v1/places/confirm")
    def confirm_place(request: PlaceConfirmRequest) -> dict:
        """Confirm one candidate from a prior resolution."""
        try: return app.state.service.confirm_place(request.resolution_id, request.candidate_id)
        except KeyError as exc: raise HTTPException(404, str(exc)) from exc

    @app.post("/v1/uploads")
    async def upload_candidates(file: UploadFile = File(...)) -> dict:
        """Upload one candidate CSV into backend-managed storage."""
        try: return app.state.service.upload_candidates(file.filename or "candidates.csv", await file.read())
        except ValueError as exc: raise HTTPException(422, str(exc)) from exc

    @app.post("/v1/runs", response_model=RunAccepted, response_model_exclude_none=True,
              status_code=status.HTTP_202_ACCEPTED)
    def start_run(request: RunRequest) -> dict:
        """Queue one confirmed task for bounded background execution."""
        try: return app.state.service.start_run(request.task, request.mode, request.candidate_upload_id)
        except KeyError as exc: raise HTTPException(404, str(exc)) from exc

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        """Return current status, progress, events, and any terminal result."""
        try: return app.state.service.run_snapshot(run_id)
        except KeyError as exc: raise HTTPException(404, str(exc)) from exc

    @app.post("/v1/runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
    def cancel_run(run_id: str) -> dict:
        """Terminate the isolated run process while retaining committed caches."""
        try: return app.state.service.cancel_run(run_id)
        except KeyError as exc: raise HTTPException(404, str(exc)) from exc

    @app.post("/v1/runs/{run_id}/revisions", response_model=RunAccepted,
              response_model_exclude_none=True, status_code=status.HTTP_202_ACCEPTED)
    def revise_run(run_id: str, request: RunRevisionRequest) -> dict:
        """Create an immutable child run from revised confirmed parameters."""
        try:
            return app.state.service.revise_run(
                run_id, request.task, request.mode, request.candidate_upload_id
            )
        except KeyError as exc: raise HTTPException(404, str(exc)) from exc
        except ValueError as exc: raise HTTPException(422, str(exc)) from exc

    @app.get("/v1/runs/{run_id}/artifacts/{name}")
    def get_artifact(run_id: str, name: str) -> FileResponse:
        """Serve one allow-listed artifact; caller paths are never accepted."""
        try: path = app.state.service.artifact(run_id, name)
        except KeyError as exc: raise HTTPException(404, str(exc)) from exc
        media = {".html": "text/html", ".png": "image/png", ".md": "text/markdown"}.get(path.suffix.lower())
        return FileResponse(path, media_type=media, filename=path.name)

    return app


app = create_app()
