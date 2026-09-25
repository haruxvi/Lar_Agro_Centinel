"""Invariant 3: every endpoint that takes a ``predio_id`` checks access to it.

An endpoint that receives a predio id and does not validate it is an IDOR
vulnerability. This walks the real application's routes, so a new endpoint
that forgets the dependency fails CI instead of shipping.

The dependency must be declared on the route itself: a guard added only at
``include_router`` time would not show up here, and is not accepted.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from fastapi import APIRouter, Depends, FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from app.main import create_app
from app.shared.dependencies import require_predio_access
from app.shared.permissions import Permission

GUARD = "require_predio_access.<locals>"


def _api_routes(routes: Iterable[object]) -> Iterator[APIRoute]:
    """Yield every APIRoute, descending into included routers and mounts.

    Since FastAPI 0.14x, ``include_router`` stores a wrapper holding the
    original router instead of copying its routes into the app. The wrapper
    is private API, so it is only relied on for ``original_router``; if that
    ever changes, ``test_the_walker_finds_the_predio_routes`` fails loudly.
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        router = getattr(route, "original_router", None)
        if router is not None:
            yield from _api_routes(router.routes)
            continue
        yield from _api_routes(getattr(route, "routes", None) or [])


def _dependency_names(dependant: Dependant) -> Iterator[str]:
    for dependency in dependant.dependencies:
        if dependency.call is not None:
            yield getattr(dependency.call, "__qualname__", "")
        yield from _dependency_names(dependency)


def _routes_with_predio_id(app: FastAPI) -> list[APIRoute]:
    return [route for route in _api_routes(app.routes) if "{predio_id}" in route.path]


def _unguarded(app: FastAPI) -> list[str]:
    return [
        f"{sorted(route.methods)} {route.path}"
        for route in _routes_with_predio_id(app)
        if not any(GUARD in name for name in _dependency_names(route.dependant))
    ]


def test_the_walker_finds_the_predio_routes() -> None:
    paths = {route.path for route in _routes_with_predio_id(create_app())}
    # Known routes: if the walker cannot see them, it cannot guard anything.
    assert "/predios/{predio_id}" in paths
    assert "/predios/{predio_id}/lotes/{lote_id}" in paths


def test_the_check_flags_an_unguarded_route() -> None:
    router = APIRouter(prefix="/predios")

    @router.get("/{predio_id}/leak")
    def leak(predio_id: str) -> str:
        return predio_id

    @router.get(
        "/{predio_id}/guarded",
        dependencies=[Depends(require_predio_access(Permission.PREDIO_VIEW))],
    )
    def guarded(predio_id: str) -> str:
        return predio_id

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    assert _unguarded(app) == ["['GET'] /predios/{predio_id}/leak"]


def test_every_route_with_a_predio_id_requires_predio_access() -> None:
    unguarded = _unguarded(create_app())
    assert unguarded == [], f"routes without require_predio_access: {unguarded}"
