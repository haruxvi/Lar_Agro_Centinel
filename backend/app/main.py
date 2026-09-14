"""FastAPI application factory."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.modules.analysis import router as analysis_router
from app.modules.audit import router as audit_router
from app.modules.auth import router as auth_router
from app.modules.health import router as health_router
from app.modules.predios import router as predios_router
from app.shared.config import Settings, get_settings

API_PREFIX = "/api/v1"

_MODULE_ROUTERS: tuple[APIRouter, ...] = (
    auth_router,
    predios_router,
    analysis_router,
    audit_router,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the ASGI application."""
    resolved = settings or get_settings()
    app = FastAPI(
        title=resolved.app_name,
        version=resolved.app_version,
        debug=resolved.debug,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    for module_router in _MODULE_ROUTERS:
        app.include_router(module_router, prefix=API_PREFIX)

    return app


app = create_app()
