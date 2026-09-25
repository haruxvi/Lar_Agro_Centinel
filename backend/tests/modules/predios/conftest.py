"""Fixtures for the predios context."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from geoalchemy2.shape import from_shape
from sqlalchemy.orm import Session

from app.modules.predios.models import Lote, Predio
from app.modules.users.models import User
from app.shared.enums import LoteType
from app.shared.geo import geojson_to_wkb, validate_geojson_geometry
from tests.fixtures import geometries as g
from tests.fixtures.predios import create_predio, derive_area_and_centroid

MakeUser = Callable[..., User]
MakePredio = Callable[..., Predio]
MakeLote = Callable[..., Lote]


@pytest.fixture
def make_user(db_session: Session) -> MakeUser:
    def _make(email: str | None = None) -> User:
        user = User(
            email=email or f"owner-{uuid.uuid4().hex[:8]}@example.cl",
            password_hash="$argon2id$placeholder",
            full_name="Predio Owner",
        )
        db_session.add(user)
        db_session.flush()
        return user

    return _make


@pytest.fixture
def make_predio(db_session: Session, make_user: MakeUser) -> MakePredio:
    def _make(
        owner: User | None = None,
        *,
        geojson: dict[str, Any] | None = None,
        slug: str | None = None,
        name: str = "Viña Santa Elena",
    ) -> Predio:
        owner = owner or make_user()
        return create_predio(db_session, owner.id, geojson=geojson, slug=slug, name=name)

    return _make


@pytest.fixture
def make_lote(db_session: Session) -> MakeLote:
    def _make(
        predio: Predio,
        *,
        geojson: dict[str, Any] | None = None,
        name: str = "Cuartel 1",
        lote_type: LoteType = LoteType.CUARTEL,
    ) -> Lote:
        shape_data = geojson or g.square(size=g.STEP / 2)
        area, centroid = derive_area_and_centroid(db_session, shape_data)
        lote = Lote(
            predio_id=predio.id,
            created_by_user_id=predio.owner_user_id,
            name=name,
            lote_type=lote_type,
            geometry=geojson_to_wkb(validate_geojson_geometry(shape_data)),
            centroid=from_shape(centroid, srid=4326),
            area_m2=area,
        )
        db_session.add(lote)
        db_session.flush()
        return lote

    return _make


__all__ = ["derive_area_and_centroid"]
