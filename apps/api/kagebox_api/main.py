"""FastAPI app factory. Every route needs a signed-in user except those in PUBLIC_ROUTES."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from kagebox_api.db import Base, Database, make_engine
from kagebox_api.deps import SAFE_METHODS, AppState
from kagebox_api.routes import auth, chapters, media, projects
from pipeline.common.settings import Settings, get_settings
from pipeline.common.storage import ObjectStore, make_store

# (method, path) pairs reachable without a session. A test asserts nothing else is.
PUBLIC_ROUTES = frozenset({("GET", "/api/health"), ("POST", "/api/auth/login")})

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}


def create_app(
    settings: Settings | None = None,
    store: ObjectStore | None = None,
    allowed_origin: str | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    key = settings.secret("media_signing_key")
    if key is None:
        raise RuntimeError("media signing key is not configured")
    db = Database(make_engine(settings))
    if settings.profile == "test":
        Base.metadata.create_all(db.engine)  # compose uses Alembic migrations

    app = FastAPI(
        title="Kagebox API",
        docs_url=None if settings.profile != "test" else "/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.upload_slots = chapters.UploadSlots(chapters.MAX_CONCURRENT_UPLOADS)
    app.state.kagebox = AppState(
        settings, db, store or make_store(settings), key.get_secret_value().encode()
    )

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Same-origin check on state-changing requests (covers login, which has no session yet).
        origin = request.headers.get("origin")
        if request.method not in SAFE_METHODS and allowed_origin and origin != allowed_origin:
            return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > settings.max_upload_bytes + 1_000_000:
            return JSONResponse({"detail": "request too large"}, status_code=413)
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    api = APIRouter(prefix="/api")

    @api.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    api.include_router(auth.router)
    api.include_router(projects.router)
    api.include_router(chapters.router)
    api.include_router(media.router)
    app.include_router(api)
    return app


def app_factory() -> FastAPI:  # uvicorn --factory entry point
    import os

    return create_app(allowed_origin=os.environ.get("KAGEBOX_ALLOWED_ORIGIN"))
