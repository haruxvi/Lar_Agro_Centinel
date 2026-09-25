"""When a concurrent request wins between a check and a write.

Every uniqueness rule is checked in the service first, for a clear error, and
enforced by a unique index as the last word. These tests switch the check off
to let the write reach the index, as a concurrent request would, and verify
the IntegrityError is translated into the same domain error, not a 500.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.modules.predios.exceptions import (
    AssignmentExistsError,
    GeoJSONImportError,
    LoteNameConflictError,
    SlugConflictError,
)
from app.modules.predios.repository import LoteRepository, PredioRepository
from app.modules.predios.service import Actor, PredioService
from app.modules.users.models import UserRole
from app.modules.users.repository import UserRoleRepository
from app.shared.roles import Role
from tests.fixtures import geometries as g
from tests.modules.predios.conftest import MakeUser

QUARTER = g.STEP / 4
HALF = g.STEP / 2
ELSEWHERE = g.square(lon=g.BASE_LON + 0.05)


@pytest.fixture
def service(db_session: Session) -> PredioService:
    return PredioService(db_session)


@pytest.fixture
def actor(db_session: Session, make_user: MakeUser) -> Actor:
    user = make_user()
    db_session.add(UserRole(user_id=user.id, role=Role.PROPIETARIO))
    db_session.flush()
    return Actor(user_id=user.id, role=Role.PROPIETARIO)


def _blind(monkeypatch: pytest.MonkeyPatch, cls: type, method: str, value: Any) -> None:
    """Make a pre-check see nothing, as if the rival write had not landed yet."""
    monkeypatch.setattr(cls, method, lambda self, *args, **kwargs: value)


def test_a_slug_taken_concurrently_on_create(
    service: PredioService, actor: Actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.create_predio(actor, geojson=g.SQUARE, fields={"name": "A", "slug": "same"})
    _blind(monkeypatch, PredioRepository, "slug_exists", False)
    with pytest.raises(SlugConflictError):
        service.create_predio(
            actor, geojson=ELSEWHERE, fields={"name": "B", "slug": "same"}
        )


def test_a_slug_taken_concurrently_on_update(
    service: PredioService, actor: Actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.create_predio(actor, geojson=g.SQUARE, fields={"name": "A", "slug": "taken"})
    other = service.create_predio(actor, geojson=ELSEWHERE, fields={"name": "B"}).predio
    _blind(monkeypatch, PredioRepository, "slug_exists", False)
    with pytest.raises(SlugConflictError):
        service.update_predio(actor, other.id, fields={"slug": "taken"})


def test_a_lote_name_taken_concurrently(
    service: PredioService, actor: Actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    predio = service.create_predio(actor, geojson=g.SQUARE, fields={"name": "P"}).predio
    service.create_lote(
        actor, predio.id, geojson=g.square(size=QUARTER), fields={"name": "Cuartel"}
    )
    _blind(monkeypatch, LoteRepository, "list_for_predio", [])
    with pytest.raises(LoteNameConflictError):
        service.create_lote(
            actor,
            predio.id,
            geojson=g.square(lon=g.BASE_LON + HALF, size=QUARTER),
            fields={"name": "Cuartel"},
        )


def test_a_lote_rename_taken_concurrently(
    service: PredioService, actor: Actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    predio = service.create_predio(actor, geojson=g.SQUARE, fields={"name": "P"}).predio
    service.create_lote(
        actor, predio.id, geojson=g.square(size=QUARTER), fields={"name": "Uno"}
    )
    other = service.create_lote(
        actor,
        predio.id,
        geojson=g.square(lon=g.BASE_LON + HALF, size=QUARTER),
        fields={"name": "Dos"},
    ).lote
    _blind(monkeypatch, LoteRepository, "list_for_predio", [])
    with pytest.raises(LoteNameConflictError):
        service.update_lote(actor, predio.id, other.id, fields={"name": "Uno"})


def test_an_import_colliding_with_a_concurrent_lote(
    service: PredioService, actor: Actor, monkeypatch: pytest.MonkeyPatch
) -> None:
    predio = service.create_predio(actor, geojson=g.SQUARE, fields={"name": "P"}).predio
    service.create_lote(
        actor, predio.id, geojson=g.square(size=QUARTER), fields={"name": "Cuartel"}
    )
    _blind(monkeypatch, LoteRepository, "list_for_predio", [])
    feature = {
        "type": "Feature",
        "geometry": g.square(lon=g.BASE_LON + HALF, size=QUARTER),
        "properties": {"name": "Cuartel"},
    }
    with pytest.raises(GeoJSONImportError) as caught:
        service.import_lotes(
            actor, predio.id, {"type": "FeatureCollection", "features": [feature]}
        )
    assert caught.value.errors == [
        {"index": None, "error": "conflicting concurrent change"}
    ]


def test_a_role_granted_concurrently(
    service: PredioService,
    actor: Actor,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predio = service.create_predio(actor, geojson=g.SQUARE, fields={"name": "P"}).predio
    worker = make_user()
    service.assign_member(actor, predio.id, email=worker.email, role=Role.APLICADOR)
    _blind(monkeypatch, UserRoleRepository, "get_predio_grant", None)
    with pytest.raises(AssignmentExistsError):
        service.assign_member(actor, predio.id, email=worker.email, role=Role.APLICADOR)
