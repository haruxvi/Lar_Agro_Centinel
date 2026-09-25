"""FastAPI application factory."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

# Registers every table so cross-module foreign keys resolve at runtime.
from app import models as _registered_models  # noqa: F401
from app.modules.analysis import router as analysis_router
from app.modules.audit import router as audit_router
from app.modules.auth import router as auth_router
from app.modules.beekeepers import router as beekeepers_router
from app.modules.captures import router as captures_router
from app.modules.devices import router as devices_router
from app.modules.health import router as health_router
from app.modules.inventory import router as inventory_router
from app.modules.meta import router as meta_router
from app.modules.notifications import router as notifications_router
from app.modules.operations import router as operations_router
from app.modules.predios import router as predios_router
from app.modules.products import router as products_router
from app.modules.responsible_mode import router as responsible_mode_router
from app.modules.users import router as users_router
from app.modules.warehouses import router as warehouses_router
from app.shared.config import Settings, get_settings
from app.shared.geo import max_geojson_bytes
from app.shared.logging import configure_logging
from app.shared.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.shared.rate_limit import limiter

API_PREFIX = "/api/v1"
META_PREFIX = "/api"

_MODULE_ROUTERS: tuple[APIRouter, ...] = (
    auth_router,
    users_router,
    predios_router,
    devices_router,
    captures_router,
    analysis_router,
    warehouses_router,
    inventory_router,
    products_router,
    operations_router,
    responsible_mode_router,
    audit_router,
    notifications_router,
    beekeepers_router,
)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the ASGI application."""
    resolved = settings or get_settings()
    configure_logging(resolved)

    app = FastAPI(
        title=resolved.app_name,
        version=resolved.app_version,
        debug=resolved.debug,
    )

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

    # Added last runs first: security headers wrap everything, so they are
    # present on rate-limit rejections and unhandled errors too.
    app.add_middleware(SlowAPIMiddleware)
    # Predio endpoints take GeoJSON: an oversized body is refused before any
    # of it is parsed.
    app.add_middleware(
        BodySizeLimitMiddleware,
        path_prefix=f"{API_PREFIX}/predios",
        max_bytes=max_geojson_bytes,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors_allowed_origins,
        allow_credentials=resolved.cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    app.include_router(health_router)
    app.include_router(meta_router, prefix=META_PREFIX)
    for module_router in _MODULE_ROUTERS:
        app.include_router(module_router, prefix=API_PREFIX)

    return app


app = create_app()
