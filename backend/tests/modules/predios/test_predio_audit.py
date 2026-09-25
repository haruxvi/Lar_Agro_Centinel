"""Audit trail of predios and lotes: the catalogue and what reaches the log."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit import events
from app.modules.audit.models import AuditLog
from app.modules.predios.service import Actor, PredioService
from app.modules.users.models import User, UserRole
from app.shared.enums import AuditEventCategory, AuditEventSeverity
from app.shared.geo import contains_geojson
from app.shared.roles import Role
from tests.fixtures import geometries as g
from tests.modules.predios.conftest import MakeUser

CATALOGUE_DOC = Path(__file__).resolve().parents[4] / "docs" / "audit-events.md"
QUARTER = g.STEP / 4

# Nothing a summary legitimately needs comes close; a geometry would.
MAX_DETAILS_BYTES = 2048


@pytest.fixture
def owner(db_session: Session, make_user: MakeUser) -> User:
    user = make_user()
    db_session.add(UserRole(user_id=user.id, role=Role.PROPIETARIO))
    db_session.flush()
    return user


@pytest.fixture
def actor(owner: User) -> Actor:
    return Actor(user_id=owner.id, role=Role.PROPIETARIO)


# --- the catalogue ------------------------------------------------------------


def _declared_predio_event_constants() -> set[str]:
    return {
        value
        for name, value in vars(events).items()
        if isinstance(value, str)
        and re.match(r"^(PREDIO|LOTE|GEOMETRY)S?_[A-Z_]+$", name)
    }


def test_every_predio_event_is_catalogued() -> None:
    assert _declared_predio_event_constants() == set(events.PREDIO_EVENTS)


@pytest.mark.parametrize(
    "event_type",
    [
        events.PREDIO_GEOMETRY_CHANGED,
        events.LOTE_GEOMETRY_CHANGED,
        events.PREDIO_DELETED,
        events.LOTE_DELETED,
        events.PREDIO_USER_ASSIGNED,
        events.PREDIO_USER_UNASSIGNED,
    ],
)
def test_what_cannot_be_undone_is_a_warning(event_type: str) -> None:
    assert events.PREDIO_EVENTS[event_type].severity is AuditEventSeverity.WARNING


def test_access_changes_are_security_events() -> None:
    for event_type in (events.PREDIO_USER_ASSIGNED, events.PREDIO_USER_UNASSIGNED):
        assert events.PREDIO_EVENTS[event_type].category is AuditEventCategory.SECURITY


def test_the_catalogue_document_matches_the_code() -> None:
    rows = {
        match.group(1): (match.group(2), match.group(3))
        for match in re.finditer(
            r"^\| `([A-Z_]+)` \| (\w+) \| (\w+) \|",
            CATALOGUE_DOC.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    }
    expected = {
        event_type: (spec.category.value, spec.severity.value)
        for event_type, spec in events.PREDIO_EVENTS.items()
    }
    assert rows == expected


# --- the geometry guard -------------------------------------------------------


@pytest.mark.parametrize(
    "details",
    [
        {"geometry": g.SQUARE},
        {"new": {"coordinates": [[0, 0]]}},
        {"items": [{"type": "Feature", "geometry": None}]},
        {"batch": ({"features": []},)},
    ],
)
def test_geojson_is_detected_at_any_depth(details: dict[str, object]) -> None:
    assert contains_geojson(details)


def test_a_geometry_summary_is_not_mistaken_for_a_geometry() -> None:
    summary = {
        "previous": {
            "area_m2": 1.0,
            "bbox": [0, 0, 1, 1],
            "vertex_count": 5,
            "geometry_type": "Polygon",
        }
    }
    assert not contains_geojson(summary)


def test_the_service_refuses_to_audit_a_geometry(
    service_and_predio: tuple[PredioService, Actor, uuid.UUID],
) -> None:
    service, actor, predio_id = service_and_predio
    with pytest.raises(ValueError, match="must not carry a geometry"):
        service._record(
            actor,
            event_type=events.PREDIO_UPDATED,
            action="test",
            predio_id=predio_id,
            target_resource_type="predio",
            target_resource_id=predio_id,
            details={"geometry": g.SQUARE},
        )


def test_the_service_refuses_an_uncatalogued_event(
    service_and_predio: tuple[PredioService, Actor, uuid.UUID],
) -> None:
    service, actor, predio_id = service_and_predio
    with pytest.raises(KeyError):
        service._record(
            actor,
            event_type="PREDIO_SOMETHING_NEW",
            action="test",
            predio_id=predio_id,
            target_resource_type="predio",
            target_resource_id=predio_id,
            details={},
        )


@pytest.fixture
def service_and_predio(
    db_session: Session, actor: Actor
) -> tuple[PredioService, Actor, uuid.UUID]:
    service = PredioService(db_session)
    predio = service.create_predio(
        actor, geojson=g.SQUARE, fields={"name": "Guard"}
    ).predio
    return service, actor, predio.id


# --- a full lifecycle, as recorded --------------------------------------------


def test_a_full_lifecycle_is_recorded_by_the_catalogue_and_without_geometry(
    db_session: Session, actor: Actor
) -> None:
    service = PredioService(db_session)
    # A detailed boundary: if any geometry leaked, the size check would see it.
    predio = service.create_predio(
        actor, geojson=g.many_vertices(2_000), fields={"name": "Ciclo completo"}
    ).predio
    service.update_predio(actor, predio.id, fields={"comuna": "Santa Cruz"})
    service.update_predio(actor, predio.id, fields={}, geojson=g.many_vertices(3_000))
    lote = service.create_lote(
        actor,
        predio.id,
        geojson=g.square(size=QUARTER),
        fields={"name": "Cuartel 1"},
    ).lote
    service.update_lote(
        actor,
        predio.id,
        lote.id,
        fields={"variety": "Carménère"},
        geojson=g.square(lat=g.BASE_LAT - QUARTER, size=QUARTER),
    )
    service.import_lotes(
        actor,
        predio.id,
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": g.square(lon=g.BASE_LON - QUARTER, size=QUARTER),
                    "properties": {"name": "Importado"},
                }
            ],
        },
    )
    for remaining in service.list_lotes(actor, predio.id):
        service.delete_lote(actor, predio.id, remaining.id)
    service.delete_predio(actor, predio.id)

    entries = list(
        db_session.execute(
            select(AuditLog)
            .where(AuditLog.predio_id == predio.id)
            .order_by(AuditLog.timestamp, AuditLog.id)
        ).scalars()
    )

    # Every predio and lote event; not the assignments nor the repairs, which
    # this lifecycle (all valid geometries) does not exercise.
    assert {entry.event_type for entry in entries} == set(events.PREDIO_EVENTS) - {
        events.PREDIO_USER_ASSIGNED,
        events.PREDIO_USER_UNASSIGNED,
        events.GEOMETRY_REPAIRED,
        events.GEOMETRY_REPAIR_ACCEPTED,
        events.GEOMETRY_REPAIR_REJECTED,
    }
    for entry in entries:
        spec = events.PREDIO_EVENTS[entry.event_type]
        assert (entry.event_category, entry.severity) == (spec.category, spec.severity)
        assert entry.actor_user_id == actor.user_id
        assert not contains_geojson(entry.details), entry.event_type
        assert len(json.dumps(entry.details)) < MAX_DETAILS_BYTES, entry.event_type


def test_a_boundary_change_records_both_summaries(
    db_session: Session, actor: Actor
) -> None:
    service = PredioService(db_session)
    predio = service.create_predio(
        actor, geojson=g.SQUARE, fields={"name": "Antes y después"}
    ).predio
    service.update_predio(actor, predio.id, fields={}, geojson=g.square(size=g.STEP * 2))

    entry = db_session.execute(
        select(AuditLog).where(
            AuditLog.event_type == events.PREDIO_GEOMETRY_CHANGED,
            AuditLog.predio_id == predio.id,
        )
    ).scalar_one()
    previous, new = entry.details["previous"], entry.details["new"]
    assert previous["bbox"] == pytest.approx(
        [g.BASE_LON, g.BASE_LAT, g.BASE_LON + g.STEP, g.BASE_LAT + g.STEP]
    )
    assert new["area_m2"] == pytest.approx(previous["area_m2"] * 4, rel=0.01)
    assert entry.details["area_delta_m2"] == pytest.approx(
        new["area_m2"] - previous["area_m2"], abs=0.01
    )
    assert previous["vertex_count"] == new["vertex_count"] == 5
