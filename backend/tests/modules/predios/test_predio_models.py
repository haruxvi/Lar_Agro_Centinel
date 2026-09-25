"""Persistence of predios and lotes in PostGIS."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from geoalchemy2.shape import from_shape, to_shape
from pyproj import Geod
from shapely.geometry import Point
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.modules.predios.models import Lote, Predio
from app.modules.users.models import User, UserPredioRole
from app.shared.enums import LoteType
from app.shared.geo import validate_geojson_geometry
from app.shared.roles import Role
from tests.fixtures import geometries as g
from tests.modules.predios.conftest import (
    MakeLote,
    MakePredio,
    MakeUser,
    derive_area_and_centroid,
)

# --- persistence and derived values -------------------------------------------


def test_a_predio_with_a_valid_geometry_persists(
    db_session: Session, make_predio: MakePredio
) -> None:
    predio = make_predio()
    db_session.expire_all()

    stored = db_session.execute(select(Predio).where(Predio.id == predio.id)).scalar_one()
    assert to_shape(stored.geometry).equals(validate_geojson_geometry(g.SQUARE))
    assert stored.is_active is True
    assert stored.deleted_at is None
    assert stored.created_at.tzinfo is not None


def test_area_agrees_with_an_independent_geodesic_calculation(
    db_session: Session,
) -> None:
    # PostGIS (geography) against pyproj (WGS84 ellipsoid): two independent
    # implementations must agree within 0.1%.
    postgis_area, _ = derive_area_and_centroid(db_session, g.SQUARE)
    geometry = validate_geojson_geometry(g.SQUARE)
    geodesic_area, _ = Geod(ellps="WGS84").geometry_area_perimeter(geometry)

    assert postgis_area == pytest.approx(abs(geodesic_area), rel=1e-3)


def test_area_is_plausible_for_a_known_square(db_session: Session) -> None:
    # 0.01 deg of latitude is ~1.109 km; 0.01 deg of longitude at 34.6 S is
    # ~0.917 km. The square is therefore close to 101.7 ha.
    area, _ = derive_area_and_centroid(db_session, g.SQUARE)
    assert area == pytest.approx(1_017_000, rel=0.01)


def test_the_stored_area_matches_st_area_on_geography(
    db_session: Session, make_predio: MakePredio
) -> None:
    predio = make_predio()
    recomputed = db_session.execute(
        text("SELECT ST_Area(geometry::geography) FROM predios WHERE id = :id"),
        {"id": predio.id},
    ).scalar_one()
    assert predio.area_m2 == pytest.approx(recomputed, rel=1e-3)


def test_the_centroid_is_the_centre_of_the_square(
    db_session: Session, make_predio: MakePredio
) -> None:
    predio = make_predio()
    centroid = to_shape(predio.centroid)
    expected = Point(g.BASE_LON + g.STEP / 2, g.BASE_LAT + g.STEP / 2)
    assert centroid.distance(expected) < 1e-9


# --- slugs --------------------------------------------------------------------


def test_a_slug_is_unique_per_owner(
    db_session: Session, make_predio: MakePredio, make_user: MakeUser
) -> None:
    owner = make_user()
    make_predio(owner, slug="santa-elena")
    with pytest.raises(IntegrityError):
        make_predio(owner, slug="santa-elena")


def test_two_owners_may_share_a_slug(
    make_predio: MakePredio, make_user: MakeUser
) -> None:
    make_predio(make_user(), slug="santa-elena")
    make_predio(make_user(), slug="santa-elena")  # does not raise


def test_a_soft_deleted_predio_releases_its_slug(
    db_session: Session, make_predio: MakePredio, make_user: MakeUser
) -> None:
    owner = make_user()
    first = make_predio(owner, slug="santa-elena")
    first.deleted_at = datetime.now(UTC)
    first.deleted_by_user_id = owner.id
    db_session.flush()

    make_predio(owner, slug="santa-elena")  # the unique index is partial


# --- soft delete --------------------------------------------------------------


def test_soft_delete_keeps_the_row(db_session: Session, make_predio: MakePredio) -> None:
    predio = make_predio()
    predio.deleted_at = datetime.now(UTC)
    predio.deleted_by_user_id = predio.owner_user_id
    db_session.flush()
    db_session.expire_all()

    stored = db_session.get(Predio, predio.id)
    assert stored is not None
    assert stored.deleted_at is not None


# --- integrity constraints ----------------------------------------------------


def test_a_role_cannot_point_at_a_predio_that_does_not_exist(
    db_session: Session, make_user: MakeUser
) -> None:
    user = make_user()
    db_session.add(
        UserPredioRole(user_id=user.id, predio_id=uuid.uuid4(), role=Role.APLICADOR)
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_a_predio_with_assigned_users_cannot_be_physically_deleted(
    db_session: Session, make_predio: MakePredio, make_user: MakeUser
) -> None:
    predio = make_predio()
    worker = make_user()
    db_session.add(
        UserPredioRole(user_id=worker.id, predio_id=predio.id, role=Role.APLICADOR)
    )
    db_session.flush()

    with pytest.raises(IntegrityError):
        db_session.execute(text("DELETE FROM predios WHERE id = :id"), {"id": predio.id})


def test_area_must_be_positive(db_session: Session, make_predio: MakePredio) -> None:
    predio = make_predio()
    with pytest.raises(IntegrityError):
        db_session.execute(
            text("UPDATE predios SET area_m2 = 0 WHERE id = :id"), {"id": predio.id}
        )


def test_only_polygonal_geometries_are_stored(
    db_session: Session, make_predio: MakePredio
) -> None:
    predio = make_predio()
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                "UPDATE predios SET geometry = "
                "ST_SetSRID(ST_MakePoint(-71.3, -34.6), 4326) "
                "WHERE id = :id"
            ),
            {"id": predio.id},
        )


def test_the_srid_is_enforced_by_the_column(
    db_session: Session, make_predio: MakePredio
) -> None:
    predio = make_predio()
    with pytest.raises(DBAPIError):
        db_session.execute(
            text(
                "UPDATE predios SET geometry = "
                "ST_Transform(geometry, 3857) WHERE id = :id"
            ),
            {"id": predio.id},
        )


# --- lotes --------------------------------------------------------------------


def test_a_lote_persists_under_its_predio(
    make_predio: MakePredio, make_lote: MakeLote
) -> None:
    lote = make_lote(make_predio(), lote_type=LoteType.POTRERO)
    assert lote.lote_type is LoteType.POTRERO
    assert lote.area_m2 > 0


def test_lote_names_are_unique_within_a_predio(
    make_predio: MakePredio, make_lote: MakeLote
) -> None:
    predio = make_predio()
    make_lote(predio, name="Cuartel 3")
    with pytest.raises(IntegrityError):
        make_lote(predio, name="Cuartel 3")


def test_different_predios_may_reuse_lote_names(
    make_predio: MakePredio, make_lote: MakeLote
) -> None:
    make_lote(make_predio(), name="Cuartel 3")
    make_lote(make_predio(), name="Cuartel 3")  # does not raise


def test_a_predio_with_lotes_cannot_be_physically_deleted(
    db_session: Session, make_predio: MakePredio, make_lote: MakeLote
) -> None:
    predio = make_predio()
    make_lote(predio)
    with pytest.raises(IntegrityError):
        db_session.execute(text("DELETE FROM predios WHERE id = :id"), {"id": predio.id})


# --- database objects ---------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "index"),
    [
        ("predios", "ix_predios_geometry"),
        ("predios", "ix_predios_centroid"),
        ("lotes", "ix_lotes_geometry"),
    ],
)
def test_spatial_indexes_exist_and_are_gist(
    db_session: Session, table: str, index: str
) -> None:
    definition = db_session.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = :table AND indexname = :index"
        ),
        {"table": table, "index": index},
    ).scalar_one()
    assert "USING gist" in definition


def test_the_slug_index_only_covers_live_rows(db_session: Session) -> None:
    definition = db_session.execute(
        text(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'uq_predios_owner_slug_active'"
        )
    ).scalar_one()
    assert "UNIQUE" in definition
    assert "WHERE (deleted_at IS NULL)" in definition


# --- runtime role -------------------------------------------------------------


def test_the_runtime_role_can_manage_predios_and_lotes(app_session: Session) -> None:
    owner = User(
        email=f"runtime-{uuid.uuid4().hex[:8]}@example.cl",
        password_hash="$argon2id$placeholder",
        full_name="Runtime Owner",
    )
    app_session.add(owner)
    app_session.flush()

    area, centroid = derive_area_and_centroid(app_session, g.SQUARE)
    from app.shared.geo import geojson_to_wkb

    predio = Predio(
        owner_user_id=owner.id,
        created_by_user_id=owner.id,
        name="Runtime",
        slug=f"runtime-{uuid.uuid4().hex[:6]}",
        geometry=geojson_to_wkb(validate_geojson_geometry(g.SQUARE)),
        centroid=from_shape(centroid, srid=4326),
        area_m2=area,
    )
    app_session.add(predio)
    app_session.flush()

    lote = Lote(
        predio_id=predio.id,
        created_by_user_id=owner.id,
        name="Cuartel runtime",
        lote_type=LoteType.CUARTEL,
        geometry=geojson_to_wkb(validate_geojson_geometry(g.square(size=g.STEP / 2))),
        centroid=from_shape(centroid, srid=4326),
        area_m2=area / 4,
    )
    app_session.add(lote)
    app_session.flush()

    lote.notes = "updated by the runtime role"
    predio.description = "updated by the runtime role"
    app_session.flush()

    app_session.execute(text("DELETE FROM lotes WHERE id = :id"), {"id": lote.id})
    app_session.execute(text("DELETE FROM predios WHERE id = :id"), {"id": predio.id})
    app_session.flush()


@pytest.mark.parametrize("table", ["predios", "lotes"])
def test_a_repair_delta_requires_the_repair_flag(
    db_session: Session, make_predio: MakePredio, make_lote: MakeLote, table: str
) -> None:
    predio = make_predio()
    row_id = predio.id if table == "predios" else make_lote(predio).id
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                f"UPDATE {table} SET geometry_repair_area_delta_m2 = 12.5 "  # noqa: S608 - fixed table names
                "WHERE id = :id"
            ),
            {"id": row_id},
        )


def test_new_geometries_are_not_marked_as_repaired(make_predio: MakePredio) -> None:
    predio = make_predio()
    assert predio.geometry_was_repaired is False
    assert predio.geometry_repair_area_delta_m2 is None
