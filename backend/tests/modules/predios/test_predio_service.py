"""Business rules of the predios service, against real PostGIS."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit import events
from app.modules.audit.models import AuditLog
from app.modules.predios.exceptions import (
    AreaOutOfBoundsError,
    GeoJSONImportError,
    InvalidSlugError,
    LoteNameConflictError,
    LoteNotContainedError,
    LoteNotFoundError,
    LotesOverlapError,
    PredioAccessDeniedError,
    PredioHasActiveLotesError,
    PredioNotFoundError,
    SlugConflictError,
)
from app.modules.predios.models import Lote
from app.modules.predios.service import (
    WARNING_OUTSIDE_CHILE,
    WARNING_OVERLAPS_PREDIO,
    WARNING_REPAIRED,
    Actor,
    PredioService,
    slugify,
)
from app.modules.users.models import User, UserPredioRole, UserRole
from app.shared.enums import AuditEventSeverity, LoteType
from app.shared.geo import InvalidGeometryError
from app.shared.pagination import PageParams
from app.shared.roles import Role
from tests.fixtures import geometries as g
from tests.modules.predios.conftest import MakeUser

HALF = g.STEP / 2
QUARTER = g.STEP / 4
DEG_PER_M_LON = 1 / 91_600


@pytest.fixture
def service(db_session: Session) -> PredioService:
    return PredioService(db_session)


@pytest.fixture
def owner(db_session: Session, make_user: MakeUser) -> User:
    """Return a user holding the global PROPIETARIO role, so they may create predios."""
    user = make_user()
    db_session.add(UserRole(user_id=user.id, role=Role.PROPIETARIO))
    db_session.flush()
    return user


@pytest.fixture
def actor(owner: User) -> Actor:
    return Actor(user_id=owner.id, role=Role.PROPIETARIO, ip="203.0.113.7")


def _audit(session: Session, event_type: str, target_id: uuid.UUID) -> list[AuditLog]:
    statement = select(AuditLog).where(
        AuditLog.event_type == event_type, AuditLog.target_resource_id == target_id
    )
    return list(session.execute(statement).scalars())


def _grant(session: Session, user: User, predio_id: uuid.UUID, role: Role) -> Actor:
    session.add(UserPredioRole(user_id=user.id, predio_id=predio_id, role=role))
    session.flush()
    return Actor(user_id=user.id, role=role)


def _create(
    service: PredioService,
    actor: Actor,
    geojson: dict[str, Any] | None = None,
    **fields: object,
) -> Any:
    fields.setdefault("name", "Viña Santa Elena")
    return service.create_predio(actor, geojson=geojson or g.SQUARE, fields=fields)


def _lote(
    service: PredioService,
    actor: Actor,
    predio_id: uuid.UUID,
    geojson: dict[str, Any] | None = None,
    name: str = "Cuartel 1",
) -> Lote:
    return service.create_lote(
        actor,
        predio_id,
        geojson=geojson or g.square(size=QUARTER),
        fields={"name": name, "lote_type": LoteType.CUARTEL},
    ).lote


# --- slugs --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Viña Santa Elena", "vina-santa-elena"),
        ("  Fundo  El Ñandú (Norte) ", "fundo-el-nandu-norte"),
        ("¡¡!!", "predio"),
    ],
)
def test_slugify(name: str, slug: str) -> None:
    assert slugify(name) == slug


# --- create -------------------------------------------------------------------


def test_create_derives_area_centroid_and_slug(
    service: PredioService, actor: Actor
) -> None:
    predio = _create(service, actor).predio

    assert predio.owner_user_id == actor.user_id
    assert predio.slug == "vina-santa-elena"
    assert predio.area_m2 == pytest.approx(1_017_000, rel=0.01)


def test_the_creator_becomes_propietario_of_the_predio(
    db_session: Session, service: PredioService, actor: Actor
) -> None:
    predio = _create(service, actor).predio
    grant = db_session.execute(
        select(UserPredioRole).where(UserPredioRole.predio_id == predio.id)
    ).scalar_one()
    assert (grant.user_id, grant.role) == (actor.user_id, Role.PROPIETARIO)


def test_create_is_audited_without_the_geometry(
    db_session: Session, service: PredioService, actor: Actor
) -> None:
    predio = _create(service, actor).predio
    [entry] = _audit(db_session, events.PREDIO_CREATED, predio.id)

    assert entry.actor_user_id == actor.user_id
    assert entry.predio_id == predio.id
    assert entry.details["area_m2"] == pytest.approx(predio.area_m2, rel=1e-6)
    assert entry.details["vertex_count"] == 5
    assert "coordinates" not in json.dumps(entry.details)


def test_a_repeated_name_gets_a_numbered_slug(
    service: PredioService, actor: Actor
) -> None:
    _create(service, actor)
    second = _create(service, actor, g.square(lon=g.BASE_LON + 0.05)).predio
    assert second.slug == "vina-santa-elena-2"


def test_an_explicit_slug_in_use_is_a_conflict(
    service: PredioService, actor: Actor
) -> None:
    _create(service, actor, slug="santa-elena")
    with pytest.raises(SlugConflictError):
        _create(service, actor, g.square(lon=g.BASE_LON + 0.05), slug="santa-elena")


@pytest.mark.parametrize("slug", ["Santa Elena", "santa_elena", "-santa", "a" * 121])
def test_a_malformed_slug_is_rejected(
    service: PredioService, actor: Actor, slug: str
) -> None:
    with pytest.raises(InvalidSlugError):
        _create(service, actor, slug=slug)


def test_a_user_without_predio_create_cannot_create(
    service: PredioService, make_user: MakeUser
) -> None:
    with pytest.raises(PredioAccessDeniedError):
        _create(service, Actor(user_id=make_user().id))


def test_a_predio_below_the_minimum_area_is_rejected(
    service: PredioService, actor: Actor
) -> None:
    # About 5 m x 5.5 m, under the 100 m2 minimum.
    with pytest.raises(AreaOutOfBoundsError) as caught:
        _create(service, actor, g.square(size=0.00005))
    assert caught.value.as_dict()["min_m2"] == 100.0


def test_an_unstorable_geometry_is_rejected(service: PredioService, actor: Actor) -> None:
    # A line is not repairable into anything. (A bowtie used to be the example
    # here; it is now repairable and asks for confirmation instead.)
    with pytest.raises(InvalidGeometryError):
        _create(service, actor, g.LINESTRING)


# --- geometry warnings --------------------------------------------------------


def test_a_predio_outside_chile_is_accepted_with_a_warning(
    service: PredioService, actor: Actor
) -> None:
    result = _create(service, actor, g.OUTSIDE_CHILE)
    assert [w.code for w in result.warnings] == [WARNING_OUTSIDE_CHILE]


def test_an_overlap_repair_is_reported_and_changes_nothing(
    service: PredioService, actor: Actor
) -> None:
    # With "structure" and the union as reference, the overlap that used to
    # lose 25% of the area is now stored as drawn.
    result = _create(service, actor, g.OVERLAPPING_MULTIPOLYGON)
    [warning] = [w for w in result.warnings if w.code == WARNING_REPAIRED]
    assert warning.detail["reason"] == "WITHIN_THRESHOLD"
    assert warning.detail["area_change_ratio"] == pytest.approx(0.0, abs=1e-6)


def test_overlapping_one_of_your_own_predios_is_a_warning(
    service: PredioService, actor: Actor
) -> None:
    first = _create(service, actor).predio
    result = _create(service, actor, g.square(lon=g.BASE_LON + HALF), name="Vecino")

    [warning] = result.warnings
    assert warning.code == WARNING_OVERLAPS_PREDIO
    assert warning.detail["predio_id"] == str(first.id)
    assert warning.detail["overlap_m2"] == pytest.approx(first.area_m2 / 2, rel=0.01)


def test_another_tenants_predio_is_never_revealed_by_an_overlap(
    db_session: Session,
    service: PredioService,
    actor: Actor,
    make_user: MakeUser,
) -> None:
    other = make_user()
    db_session.add(UserRole(user_id=other.id, role=Role.PROPIETARIO))
    db_session.flush()
    _create(service, Actor(user_id=other.id))

    result = _create(service, actor, g.square(lon=g.BASE_LON + HALF))
    assert result.warnings == []


# --- read ---------------------------------------------------------------------


def test_a_stranger_cannot_read_a_predio(
    service: PredioService, actor: Actor, make_user: MakeUser
) -> None:
    predio = _create(service, actor).predio
    with pytest.raises(PredioAccessDeniedError):
        service.get_predio(Actor(user_id=make_user().id), predio.id)


def test_a_stranger_cannot_tell_whether_a_predio_exists(
    service: PredioService, make_user: MakeUser
) -> None:
    with pytest.raises(PredioAccessDeniedError):
        service.get_predio(Actor(user_id=make_user().id), uuid.uuid4())


def test_list_returns_only_the_predios_the_actor_may_view(
    db_session: Session, service: PredioService, actor: Actor, make_user: MakeUser
) -> None:
    mine = _create(service, actor).predio
    worker = make_user()
    _grant(db_session, worker, mine.id, Role.APLICADOR)
    bodeguero = make_user()
    _grant(db_session, bodeguero, mine.id, Role.BODEGUERO)  # no PREDIO_VIEW

    assert service.list_predios(actor, PageParams()).items == [mine]
    assert service.list_predios(Actor(user_id=worker.id), PageParams()).items == [mine]
    assert service.list_predios(Actor(user_id=bodeguero.id), PageParams()).total == 0
    assert service.list_predios(Actor(user_id=make_user().id), PageParams()).total == 0


def test_a_global_auditor_lists_every_predio(
    db_session: Session, service: PredioService, actor: Actor, make_user: MakeUser
) -> None:
    predio = _create(service, actor).predio
    auditor = make_user()
    db_session.add(UserRole(user_id=auditor.id, role=Role.AUDITOR))
    db_session.flush()

    page = service.list_predios(Actor(user_id=auditor.id), PageParams(page_size=100))
    assert predio in page.items


# --- update -------------------------------------------------------------------


def test_updating_attributes_is_audited_by_field_name(
    db_session: Session, service: PredioService, actor: Actor
) -> None:
    predio = _create(service, actor).predio
    service.update_predio(
        actor, predio.id, fields={"name": "Viña Los Robles", "comuna": "Santa Cruz"}
    )

    [entry] = _audit(db_session, events.PREDIO_UPDATED, predio.id)
    assert entry.details == {"fields": ["comuna", "name"]}
    assert _audit(db_session, events.PREDIO_GEOMETRY_CHANGED, predio.id) == []


def test_a_boundary_change_is_a_warning_with_before_and_after(
    db_session: Session, service: PredioService, actor: Actor
) -> None:
    predio = _create(service, actor).predio
    old_area = predio.area_m2
    larger = g.square(size=g.STEP * 2)

    service.update_predio(actor, predio.id, fields={}, geojson=larger)

    assert predio.area_m2 == pytest.approx(old_area * 4, rel=0.01)
    [entry] = _audit(db_session, events.PREDIO_GEOMETRY_CHANGED, predio.id)
    assert entry.severity is AuditEventSeverity.WARNING
    assert entry.details["previous"]["area_m2"] == pytest.approx(old_area, rel=1e-6)
    assert entry.details["new"]["area_m2"] == pytest.approx(predio.area_m2, rel=1e-6)
    assert "coordinates" not in json.dumps(entry.details)


def test_a_boundary_that_leaves_a_lote_outside_is_rejected(
    service: PredioService, actor: Actor
) -> None:
    predio = _create(service, actor).predio
    lote = _lote(service, actor, predio.id, g.square(lon=g.BASE_LON + HALF, size=QUARTER))

    with pytest.raises(LoteNotContainedError) as caught:
        service.update_predio(actor, predio.id, fields={}, geojson=g.square(size=HALF))
    assert caught.value.lote_id == lote.id


def test_an_aplicador_cannot_edit_the_predio(
    db_session: Session, service: PredioService, actor: Actor, make_user: MakeUser
) -> None:
    predio = _create(service, actor).predio
    worker = _grant(db_session, make_user(), predio.id, Role.APLICADOR)
    with pytest.raises(PredioAccessDeniedError):
        service.update_predio(worker, predio.id, fields={"name": "Mío"})


# --- delete -------------------------------------------------------------------


def test_a_predio_with_lotes_cannot_be_deleted(
    service: PredioService, actor: Actor
) -> None:
    predio = _create(service, actor).predio
    _lote(service, actor, predio.id)
    with pytest.raises(PredioHasActiveLotesError) as caught:
        service.delete_predio(actor, predio.id)
    assert caught.value.active_lotes == 1


def test_delete_is_soft_and_audited(
    db_session: Session, service: PredioService, actor: Actor
) -> None:
    predio = _create(service, actor).predio
    service.delete_predio(actor, predio.id)

    assert predio.deleted_at is not None
    with pytest.raises(PredioNotFoundError):
        service.get_predio(actor, predio.id)
    [entry] = _audit(db_session, events.PREDIO_DELETED, predio.id)
    assert entry.severity is AuditEventSeverity.WARNING


def test_deleting_the_last_lote_unblocks_the_predio(
    service: PredioService, actor: Actor
) -> None:
    predio = _create(service, actor).predio
    lote = _lote(service, actor, predio.id)
    service.delete_lote(actor, predio.id, lote.id)
    service.delete_predio(actor, predio.id)  # does not raise


# --- lotes --------------------------------------------------------------------


def test_a_lote_inside_the_predio_is_created_and_audited(
    db_session: Session, service: PredioService, actor: Actor
) -> None:
    predio = _create(service, actor).predio
    lote = _lote(service, actor, predio.id)

    assert lote.area_m2 == pytest.approx(predio.area_m2 / 16, rel=0.01)
    [entry] = _audit(db_session, events.LOTE_CREATED, lote.id)
    assert entry.predio_id == predio.id
    assert entry.details["lote_type"] == "CUARTEL"


def test_a_lote_outside_the_predio_is_rejected(
    service: PredioService, actor: Actor
) -> None:
    predio = _create(service, actor).predio
    outside = g.square(lon=g.BASE_LON - 50 * DEG_PER_M_LON, size=QUARTER)
    with pytest.raises(LoteNotContainedError) as caught:
        _lote(service, actor, predio.id, outside)
    assert caught.value.outside_m2 > 0


def test_a_lote_past_the_border_within_tolerance_is_accepted(
    service: PredioService, actor: Actor
) -> None:
    predio = _create(service, actor).predio
    _lote(service, actor, predio.id, g.square(lon=g.BASE_LON - 2 * DEG_PER_M_LON))


def test_overlapping_sibling_lotes_are_rejected(
    service: PredioService, actor: Actor
) -> None:
    predio = _create(service, actor).predio
    first = _lote(service, actor, predio.id)
    with pytest.raises(LotesOverlapError) as caught:
        _lote(
            service,
            actor,
            predio.id,
            g.square(lon=g.BASE_LON + QUARTER / 2, size=QUARTER),
            name="Cuartel 2",
        )
    assert [lote_id for lote_id, _ in caught.value.overlaps] == [first.id]


def test_lotes_sharing_a_border_are_accepted(
    service: PredioService, actor: Actor
) -> None:
    predio = _create(service, actor).predio
    _lote(service, actor, predio.id)
    _lote(
        service,
        actor,
        predio.id,
        g.square(lon=g.BASE_LON + QUARTER, size=QUARTER),
        name="Cuartel 2",
    )


def test_lote_names_are_unique_within_the_predio(
    service: PredioService, actor: Actor
) -> None:
    predio = _create(service, actor).predio
    _lote(service, actor, predio.id)
    with pytest.raises(LoteNameConflictError):
        _lote(service, actor, predio.id, g.square(lon=g.BASE_LON + HALF, size=QUARTER))


def test_a_lote_cannot_be_reached_through_another_predio(
    service: PredioService, actor: Actor
) -> None:
    home = _create(service, actor).predio
    other = _create(service, actor, g.square(lon=g.BASE_LON + 0.05), name="Otro").predio
    lote = _lote(service, actor, home.id)

    with pytest.raises(LoteNotFoundError):
        service.get_lote(actor, other.id, lote.id)
    with pytest.raises(LoteNotFoundError):
        service.delete_lote(actor, other.id, lote.id)


def test_moving_a_lote_does_not_collide_with_itself(
    db_session: Session, service: PredioService, actor: Actor
) -> None:
    predio = _create(service, actor).predio
    lote = _lote(service, actor, predio.id)
    moved = g.square(lon=g.BASE_LON + QUARTER / 2, size=QUARTER)

    service.update_lote(actor, predio.id, lote.id, fields={}, geojson=moved)
    [entry] = _audit(db_session, events.LOTE_GEOMETRY_CHANGED, lote.id)
    assert entry.severity is AuditEventSeverity.WARNING


def test_the_summary_adds_up(service: PredioService, actor: Actor) -> None:
    predio = _create(service, actor).predio
    first = _lote(service, actor, predio.id)
    second = _lote(
        service,
        actor,
        predio.id,
        g.square(lon=g.BASE_LON + HALF, size=QUARTER),
        name="Cuartel 2",
    )

    summary = service.summarize(actor, predio.id)
    assert summary.lotes_count == 2
    assert summary.lotes_area_m2 == pytest.approx(first.area_m2 + second.area_m2)
    assert summary.unassigned_area_m2 == pytest.approx(
        predio.area_m2 - summary.lotes_area_m2
    )
    assert summary.coverage_ratio == pytest.approx(0.125, rel=0.01)
    assert summary.lotes_by_type == {LoteType.CUARTEL: 2}


# --- GeoJSON import -----------------------------------------------------------


def _feature(geojson: dict[str, Any], name: str, **properties: object) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": geojson,
        "properties": {"name": name, **properties},
    }


def _collection(*features: dict[str, Any]) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": list(features)}


def test_an_import_creates_every_lote(
    db_session: Session, service: PredioService, actor: Actor
) -> None:
    predio = _create(service, actor).predio
    results = service.import_lotes(
        actor,
        predio.id,
        _collection(
            _feature(g.square(size=QUARTER), "Cuartel 1", lote_type="CUARTEL"),
            _feature(
                g.square(lon=g.BASE_LON + HALF, size=QUARTER),
                "Potrero",
                lote_type="POTRERO",
            ),
        ),
    )

    assert [r.lote.name for r in results] == ["Cuartel 1", "Potrero"]
    assert results[1].lote.lote_type is LoteType.POTRERO
    [entry] = _audit(db_session, events.LOTES_IMPORTED, predio.id)
    assert entry.details["count"] == 2


def test_one_bad_feature_rejects_the_whole_import(
    service: PredioService, actor: Actor
) -> None:
    predio = _create(service, actor).predio
    outside = g.square(lon=g.BASE_LON - 0.05, size=QUARTER)

    with pytest.raises(GeoJSONImportError) as caught:
        service.import_lotes(
            actor,
            predio.id,
            _collection(
                _feature(g.square(size=QUARTER), "Cuartel 1"),
                _feature(outside, "Fuera"),
                _feature(g.LINESTRING, "Inválido"),
                _feature(g.square(lon=g.BASE_LON + HALF, size=QUARTER), "Cuartel 1"),
            ),
        )

    errors = {e["index"]: e["error"] for e in caught.value.errors}
    assert errors == {
        1: "LoteNotContainedError",
        2: "InvalidGeometryError",
        3: "LoteNameConflictError",
    }
    assert service.list_lotes(actor, predio.id) == []


def test_features_that_overlap_each_other_are_rejected(
    service: PredioService, actor: Actor
) -> None:
    predio = _create(service, actor).predio
    with pytest.raises(GeoJSONImportError) as caught:
        service.import_lotes(
            actor,
            predio.id,
            _collection(
                _feature(g.square(size=QUARTER), "A"),
                _feature(g.square(lon=g.BASE_LON + QUARTER / 2, size=QUARTER), "B"),
            ),
        )
    assert caught.value.errors == [
        {"index": 1, "error": "LotesOverlapError", "overlaps_feature": 0}
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "Feature"},
        {"type": "FeatureCollection", "features": []},
        {"type": "FeatureCollection", "features": [{"type": "Feature"}] * 501},
    ],
)
def test_malformed_imports_are_rejected(
    service: PredioService, actor: Actor, payload: dict[str, Any]
) -> None:
    predio = _create(service, actor).predio
    with pytest.raises(GeoJSONImportError):
        service.import_lotes(actor, predio.id, payload)


def test_a_global_propietario_does_not_see_other_tenants_predios(
    db_session: Session, service: PredioService, actor: Actor, make_user: MakeUser
) -> None:
    mine = _create(service, actor).predio
    rival = make_user()
    db_session.add(UserRole(user_id=rival.id, role=Role.PROPIETARIO))
    db_session.flush()
    rival_actor = Actor(user_id=rival.id, role=Role.PROPIETARIO)

    assert mine not in service.list_predios(rival_actor, PageParams(page_size=100)).items
    with pytest.raises(PredioAccessDeniedError):
        service.update_predio(rival_actor, mine.id, fields={"name": "Mío"})
    with pytest.raises(PredioAccessDeniedError):
        service.delete_predio(rival_actor, mine.id)
