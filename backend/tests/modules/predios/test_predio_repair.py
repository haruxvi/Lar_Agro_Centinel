"""Automatic geometry repair: when it is stored silently, and when it is asked.

Areas here are real PostGIS measurements (geography, m2). The protruding-hole
fixtures change the area by an exact, known amount, so each branch of the
threshold is exercised on purpose rather than by coincidence.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit import events
from app.modules.audit.models import AuditLog
from app.modules.predios.exceptions import (
    GeoJSONImportError,
    GeometryRepairExceedsThresholdError,
)
from app.modules.predios.models import Lote, Predio
from app.modules.predios.service import Actor, PredioService
from app.modules.users.models import User, UserRole
from app.shared.enums import AuditEventSeverity
from app.shared.geo import RepairVerdict, contains_geojson
from app.shared.roles import Role
from tests.fixtures import geometries as g
from tests.modules.predios.conftest import MakeUser

QUARTER = g.STEP / 4
HALF = g.STEP / 2

# Exact area changes, measured in PostGIS when calibrating the threshold.
SMALL_REPAIR = g.protruding_hole(side_m=1000, hole_height_m=4, inside_m=1, outside_m=1)
FLOOR_BRANCH = g.protruding_hole(
    side_m=22.3607, hole_height_m=10, inside_m=1, outside_m=1
)
RATIO_BRANCH = g.protruding_hole(
    side_m=670.82, hole_height_m=100, inside_m=2, outside_m=2
)
ABOVE_THRESHOLD = g.protruding_hole(
    side_m=1000, hole_height_m=200, inside_m=50, outside_m=100
)


@pytest.fixture
def service(db_session: Session) -> PredioService:
    return PredioService(db_session)


@pytest.fixture
def actor(db_session: Session, make_user: MakeUser) -> Actor:
    user: User = make_user()
    db_session.add(UserRole(user_id=user.id, role=Role.PROPIETARIO))
    db_session.flush()
    return Actor(user_id=user.id, role=Role.PROPIETARIO)


def _create(
    service: PredioService, actor: Actor, geojson: dict[str, object], **kwargs: bool
) -> Predio:
    return service.create_predio(
        actor, geojson=geojson, fields={"name": f"P {uuid.uuid4().hex[:6]}"}, **kwargs
    ).predio


def _events(session: Session, event_type: str, actor: Actor) -> list[AuditLog]:
    statement = select(AuditLog).where(
        AuditLog.event_type == event_type, AuditLog.actor_user_id == actor.user_id
    )
    return list(session.execute(statement).scalars())


# --- accepted without confirmation --------------------------------------------


def test_the_overlap_case_is_stored_as_the_union_and_flagged(
    db_session: Session, service: PredioService, actor: Actor
) -> None:
    predio = _create(service, actor, g.OVERLAPPING_MULTIPOLYGON)

    assert predio.geometry_was_repaired is True
    assert predio.geometry_repair_area_delta_m2 == pytest.approx(0.0, abs=0.01)
    [entry] = _events(db_session, events.GEOMETRY_REPAIRED, actor)
    assert entry.severity is AuditEventSeverity.INFO
    assert entry.predio_id == predio.id


def test_a_small_repair_is_accepted_without_the_flag(
    service: PredioService, actor: Actor
) -> None:
    # The replacement for the "duplicate vertex" case, which GEOS considers
    # valid and so never exercised a repair: a real repair of a few m2.
    predio = _create(service, actor, SMALL_REPAIR)
    assert predio.geometry_repair_area_delta_m2 == pytest.approx(4.0, abs=0.5)


def test_the_absolute_floor_forgives_a_small_parcel(
    service: PredioService, actor: Actor
) -> None:
    # ~500 m2 parcel, +10 m2: a 2% ratio, over 1%, but under the 50 m2 floor.
    predio = _create(service, actor, FLOOR_BRANCH)
    assert predio.area_m2 == pytest.approx(490.6, rel=0.01)
    assert predio.geometry_repair_area_delta_m2 == pytest.approx(10.0, abs=0.5)


def test_the_ratio_forgives_a_large_predio(service: PredioService, actor: Actor) -> None:
    # ~45 ha, +200 m2: over the 50 m2 floor, but a 0.04% ratio.
    predio = _create(service, actor, RATIO_BRANCH)
    assert predio.area_m2 == pytest.approx(450_377, rel=0.01)
    assert predio.geometry_repair_area_delta_m2 == pytest.approx(200.0, abs=2)


# --- confirmation required ----------------------------------------------------


def test_a_repair_over_the_threshold_asks_with_a_preview(
    db_session: Session, service: PredioService, actor: Actor
) -> None:
    with pytest.raises(GeometryRepairExceedsThresholdError) as caught:
        _create(service, actor, ABOVE_THRESHOLD)

    payload = caught.value.as_dict()
    detail = payload["detail"]
    assert payload["error"] == "GeometryRepairExceedsThreshold"
    assert isinstance(detail, dict)
    assert detail["reason"] == "AREA_CHANGE_ABOVE_THRESHOLD"
    assert detail["area_change_m2"] == pytest.approx(20_026, rel=0.01)
    assert detail["area_change_ratio"] == pytest.approx(0.0206, abs=0.001)
    assert detail["threshold_ratio"] == 0.01
    assert detail["repaired_geometry"]["type"] == "Polygon"
    assert "2.1%" in detail["message"]
    # Nothing stored; the rejection itself is on record.
    assert (
        db_session.execute(
            select(Predio).where(Predio.owner_user_id == actor.user_id)
        ).first()
        is None
    )
    [entry] = _events(db_session, events.GEOMETRY_REPAIR_REJECTED, actor)
    assert entry.severity is AuditEventSeverity.INFO
    assert entry.predio_id is None  # the predio was never created


def test_confirming_stores_the_repair_and_records_a_warning(
    db_session: Session, service: PredioService, actor: Actor
) -> None:
    predio = _create(service, actor, ABOVE_THRESHOLD, accept_repair=True)

    assert predio.geometry_was_repaired is True
    assert predio.geometry_repair_area_delta_m2 == pytest.approx(20_026, rel=0.01)
    [entry] = _events(db_session, events.GEOMETRY_REPAIR_ACCEPTED, actor)
    assert entry.severity is AuditEventSeverity.WARNING
    assert entry.details["area_change_ratio"] == pytest.approx(0.0206, abs=0.001)
    assert entry.details["threshold_ratio"] == 0.01
    assert _events(db_session, events.GEOMETRY_REPAIRED, actor) == []


def test_a_symmetric_bowtie_is_not_measurable_by_zero_area(
    service: PredioService, actor: Actor
) -> None:
    with pytest.raises(GeometryRepairExceedsThresholdError) as caught:
        _create(service, actor, g.SELF_INTERSECTING)
    assessment = caught.value.assessment
    assert assessment.verdict is RepairVerdict.NOT_MEASURABLE_ZERO_AREA
    assert caught.value.as_dict()["detail"]["area_change_ratio"] is None  # type: ignore[index]


def test_an_asymmetric_bowtie_asks_even_though_its_ratio_looks_harmless(
    service: PredioService, actor: Actor
) -> None:
    # Adjustment A: the naive ratio is ~0.08%, under the 1% threshold. Without
    # the self-intersection detector this would be stored silently.
    with pytest.raises(GeometryRepairExceedsThresholdError) as caught:
        _create(service, actor, g.ASYMMETRIC_BOWTIE)
    assessment = caught.value.assessment
    assert assessment.verdict is RepairVerdict.NOT_MEASURABLE_SELF_INTERSECTION
    naive_ratio = abs(assessment.change_m2) / assessment.area_before_m2
    assert naive_ratio < 0.01
    assert caught.value.as_dict()["detail"]["area_change_ratio"] is None  # type: ignore[index]


def test_a_confirmed_unmeasurable_repair_stores_no_delta(
    service: PredioService, actor: Actor
) -> None:
    predio = _create(service, actor, g.SELF_INTERSECTING, accept_repair=True)
    assert predio.geometry_was_repaired is True
    assert predio.geometry_repair_area_delta_m2 is None


# --- updates reset the flag ---------------------------------------------------


def test_a_valid_boundary_resets_the_repair_flag(
    service: PredioService, actor: Actor
) -> None:
    predio = _create(service, actor, ABOVE_THRESHOLD, accept_repair=True)
    service.update_predio(actor, predio.id, fields={}, geojson=g.SQUARE)

    assert predio.geometry_was_repaired is False
    assert predio.geometry_repair_area_delta_m2 is None


def test_an_update_needing_confirmation_changes_nothing(
    service: PredioService, actor: Actor
) -> None:
    predio = _create(service, actor, g.SQUARE)
    area = predio.area_m2
    with pytest.raises(GeometryRepairExceedsThresholdError):
        service.update_predio(actor, predio.id, fields={}, geojson=ABOVE_THRESHOLD)
    assert predio.area_m2 == area
    assert predio.geometry_was_repaired is False


# --- lotes ----------------------------------------------------------------------

# ~400 m square lote inside the SQUARE predio, +2,000 m2 (1.25%): over both bounds.
LOTE_ABOVE = g.protruding_hole(side_m=400, hole_height_m=100, inside_m=20, outside_m=20)


def test_lotes_follow_the_same_policy(
    db_session: Session, service: PredioService, actor: Actor
) -> None:
    predio = _create(service, actor, g.SQUARE)
    fields = {"name": "Cuartel reparado"}

    with pytest.raises(GeometryRepairExceedsThresholdError):
        service.create_lote(actor, predio.id, geojson=LOTE_ABOVE, fields=fields)
    lote = service.create_lote(
        actor, predio.id, geojson=LOTE_ABOVE, fields=fields, accept_repair=True
    ).lote

    assert lote.geometry_was_repaired is True
    assert lote.geometry_repair_area_delta_m2 == pytest.approx(2_000, rel=0.02)
    [accepted] = _events(db_session, events.GEOMETRY_REPAIR_ACCEPTED, actor)
    assert (accepted.target_resource_type, accepted.target_resource_id) == (
        "lote",
        lote.id,
    )

    service.update_lote(
        actor, predio.id, lote.id, fields={}, geojson=g.square(size=QUARTER)
    )
    assert lote.geometry_was_repaired is False
    assert lote.geometry_repair_area_delta_m2 is None


# --- import ---------------------------------------------------------------------


def _collection(*features: dict[str, object], **extra: object) -> dict[str, object]:
    return {"type": "FeatureCollection", "features": list(features), **extra}


def _feature(geometry: dict[str, object], name: str) -> dict[str, object]:
    return {"type": "Feature", "geometry": geometry, "properties": {"name": name}}


# Beside the QUARTER lote at the base corner, so the two do not overlap.
LOTE_ABOVE_EAST = g.protruding_hole(
    side_m=400, hole_height_m=100, inside_m=20, outside_m=20, lon=g.BASE_LON + HALF
)


def test_an_import_aborts_whole_when_one_repair_needs_confirmation(
    db_session: Session, service: PredioService, actor: Actor
) -> None:
    predio = _create(service, actor, g.SQUARE)
    collection = _collection(
        _feature(g.square(size=QUARTER), "Normal"),
        _feature(LOTE_ABOVE_EAST, "Reparado"),
    )

    with pytest.raises(GeoJSONImportError) as caught:
        service.import_lotes(actor, predio.id, collection)

    [error] = caught.value.errors
    assert error["index"] == 1
    assert error["error"] == "GeometryRepairExceedsThreshold"
    assert (
        db_session.execute(select(Lote).where(Lote.predio_id == predio.id)).first()
        is None
    )
    assert len(_events(db_session, events.GEOMETRY_REPAIR_REJECTED, actor)) == 1

    results = service.import_lotes(actor, predio.id, collection, accept_repair=True)
    assert [r.lote.geometry_was_repaired for r in results] == [False, True]
    assert len(_events(db_session, events.GEOMETRY_REPAIR_ACCEPTED, actor)) == 1


# --- no geometry in the audit trail -------------------------------------------


def test_no_repair_event_carries_a_geometry(
    db_session: Session, service: PredioService, actor: Actor
) -> None:
    _create(service, actor, g.OVERLAPPING_MULTIPOLYGON)
    with pytest.raises(GeometryRepairExceedsThresholdError):
        _create(service, actor, ABOVE_THRESHOLD)
    _create(service, actor, ABOVE_THRESHOLD, accept_repair=True)

    repair_events = [
        entry
        for event_type in (
            events.GEOMETRY_REPAIRED,
            events.GEOMETRY_REPAIR_REJECTED,
            events.GEOMETRY_REPAIR_ACCEPTED,
        )
        for entry in _events(db_session, event_type, actor)
    ]
    assert len(repair_events) == 3
    for entry in repair_events:
        assert not contains_geojson(entry.details)
        assert "bbox" in entry.details
