"""Persistence of analyses, per-lote statistics and anomalies in PostGIS.

Each invariant is tested at the database level, by writing what the service
would never write: the constraint has to hold even when the code does not.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest
from geoalchemy2.shape import from_shape
from shapely.geometry import Point, box
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.modules.analysis.models import Analysis, AnalysisLoteStats, Anomaly
from app.modules.predios.models import Lote, Predio
from app.modules.users.models import User
from app.shared.enums import (
    AnalysisStatus,
    AnalysisType,
    AnomalyReviewStatus,
    AnomalySeverity,
    LoteStatsExclusion,
)
from tests.fixtures import geometries as g
from tests.fixtures.predios import create_lote, create_predio

SCENE = "S2A_MSIL2A_20260915T143731_N0511_R096_T19HCC_20260915T191008"


@pytest.fixture
def user(db_session: Session) -> User:
    person = User(
        email=f"analyst-{uuid.uuid4().hex[:8]}@example.cl",
        password_hash="$argon2id$placeholder",
        full_name="Analista",
    )
    db_session.add(person)
    db_session.flush()
    return person


@pytest.fixture
def predio(db_session: Session, user: User) -> Predio:
    return create_predio(db_session, user.id)


@pytest.fixture
def lote(db_session: Session, predio: Predio) -> Lote:
    return create_lote(db_session, predio)


def _analysis(predio: Predio, user: User, **overrides: Any) -> Analysis:
    values: dict[str, Any] = {
        "predio_id": predio.id,
        "analysis_type": AnalysisType.NDVI,
        "status": AnalysisStatus.PENDING,
        "requested_date_from": date(2026, 9, 1),
        "requested_date_to": date(2026, 9, 30),
        "requested_by_user_id": user.id,
    }
    values.update(overrides)
    return Analysis(**values)


def _completed(predio: Predio, user: User, scene_id: str = SCENE) -> Analysis:
    return _analysis(
        predio,
        user,
        status=AnalysisStatus.COMPLETED,
        scene_id=scene_id,
        scene_date=date(2026, 9, 15),
        completed_at=datetime.now(UTC),
    )


def _add(session: Session, *rows: object) -> None:
    session.add_all(rows)
    session.flush()


# --- analyses -----------------------------------------------------------------------


def test_a_pending_analysis_persists(
    db_session: Session, predio: Predio, user: User
) -> None:
    analysis = _analysis(predio, user)
    _add(db_session, analysis)
    db_session.expire_all()

    stored = db_session.get(Analysis, analysis.id)
    assert stored is not None
    assert stored.status is AnalysisStatus.PENDING
    assert stored.requested_at.tzinfo is not None
    assert stored.scene_id is None


def test_error_detail_keeps_the_evaluated_scenes(
    db_session: Session, predio: Predio, user: User
) -> None:
    detail = {
        "scenes_evaluated": 2,
        "evaluated": [
            {"date": "2026-09-20", "cloud_fraction": 0.85},
            {"date": "2026-09-15", "cloud_fraction": 0.47},
        ],
    }
    analysis = _analysis(
        predio, user, status=AnalysisStatus.NO_SUITABLE_SCENE, error_detail=detail
    )
    _add(db_session, analysis)
    db_session.expire_all()
    assert db_session.get(Analysis, analysis.id).error_detail == detail  # type: ignore[union-attr]


def test_the_same_scene_is_never_completed_twice(
    db_session: Session, predio: Predio, user: User
) -> None:
    # Idempotency: recomputing an existing scene would only burn quota.
    _add(db_session, _completed(predio, user))
    with pytest.raises(IntegrityError):
        _add(db_session, _completed(predio, user))


def test_another_scene_or_a_failure_on_the_same_scene_is_allowed(
    db_session: Session, predio: Predio, user: User
) -> None:
    _add(db_session, _completed(predio, user))
    _add(db_session, _completed(predio, user, scene_id="S2B_another_scene"))
    _add(
        db_session,
        _analysis(
            predio,
            user,
            status=AnalysisStatus.FAILED,
            scene_id=SCENE,
            error_code="TIMEOUT",
        ),
    )


def test_two_analyses_of_a_type_cannot_be_in_flight_for_one_predio(
    db_session: Session, predio: Predio, user: User
) -> None:
    _add(db_session, _analysis(predio, user))
    with pytest.raises(IntegrityError):
        _add(db_session, _analysis(predio, user, status=AnalysisStatus.RUNNING))


def test_a_new_analysis_may_start_once_the_previous_one_finished(
    db_session: Session, predio: Predio, user: User
) -> None:
    first = _analysis(predio, user)
    _add(db_session, first)
    first.status = AnalysisStatus.FAILED
    first.error_code = "TIMEOUT"
    db_session.flush()
    _add(db_session, _analysis(predio, user))  # does not raise


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"status": AnalysisStatus.COMPLETED}, "ck_analyses_completed_has_scene"),
        ({"status": AnalysisStatus.FAILED}, "ck_analyses_failed_has_error_code"),
        (
            {
                "requested_date_from": date(2026, 9, 30),
                "requested_date_to": date(2026, 9, 1),
            },
            "ck_analyses_date_range_ordered",
        ),
        ({"cloud_coverage": 1.5}, "ck_analyses_cloud_coverage_ratio"),
        ({"valid_pixels_ratio": -0.1}, "ck_analyses_valid_pixels_ratio"),
        ({"processing_units_spent": -1.0}, "ck_analyses_pu_non_negative"),
    ],
)
def test_inconsistent_analyses_are_refused(
    db_session: Session,
    predio: Predio,
    user: User,
    overrides: dict[str, Any],
    constraint: str,
) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        _add(db_session, _analysis(predio, user, **overrides))


# --- duplicates and supersession ----------------------------------------------------


def test_a_superseded_result_and_its_replacement_coexist_on_one_scene(
    db_session: Session, predio: Predio, user: User
) -> None:
    old = _completed(predio, user)
    _add(db_session, old)
    replacement = _analysis(predio, user, status=AnalysisStatus.RUNNING)
    _add(db_session, replacement)

    # The worker's order: supersede, flush, then complete the replacement.
    old.superseded_by_analysis_id = replacement.id
    db_session.flush()
    replacement.status = AnalysisStatus.COMPLETED
    replacement.scene_id = SCENE
    replacement.scene_date = date(2026, 9, 15)
    replacement.completed_at = datetime.now(UTC)
    db_session.flush()

    rows = db_session.execute(
        select(Analysis).where(
            Analysis.scene_id == SCENE, Analysis.status == AnalysisStatus.COMPLETED
        )
    ).scalars()
    assert {row.id for row in rows} == {old.id, replacement.id}


def test_only_one_vigente_result_per_scene_even_after_superseding(
    db_session: Session, predio: Predio, user: User
) -> None:
    old = _completed(predio, user)
    replacement = _analysis(predio, user, status=AnalysisStatus.RUNNING)
    _add(db_session, old, replacement)
    old.superseded_by_analysis_id = replacement.id
    db_session.flush()

    _add(db_session, _completed(predio, user))  # the new vigente one: allowed
    with pytest.raises(IntegrityError, match="uq_analyses_completed_scene"):
        _add(db_session, _completed(predio, user))  # a second vigente one: refused


def test_a_duplicate_points_at_the_analysis_it_resolved_to(
    db_session: Session, predio: Predio, user: User
) -> None:
    existing = _completed(predio, user)
    _add(db_session, existing)
    duplicate = _analysis(
        predio,
        user,
        status=AnalysisStatus.DUPLICATE_SCENE,
        resolved_to_analysis_id=existing.id,
        scene_id=SCENE,
    )
    _add(db_session, duplicate)
    assert duplicate.resolved_to_analysis_id == existing.id


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        (
            {"status": AnalysisStatus.DUPLICATE_SCENE},
            "ck_analyses_duplicate_resolved",
        ),
        (
            {"status": AnalysisStatus.FAILED, "error_code": "X", "resolved_to": True},
            "ck_analyses_duplicate_resolved",
        ),
        (
            {"status": AnalysisStatus.FAILED, "error_code": "X", "superseded_by": True},
            "ck_analyses_superseded_only_completed",
        ),
    ],
)
def test_inconsistent_links_between_analyses_are_refused(
    db_session: Session,
    predio: Predio,
    user: User,
    overrides: dict[str, Any],
    constraint: str,
) -> None:
    other = _completed(predio, user)
    _add(db_session, other)
    values = dict(overrides)
    if values.pop("resolved_to", False):
        values["resolved_to_analysis_id"] = other.id
    if values.pop("superseded_by", False):
        values["superseded_by_analysis_id"] = other.id
    with pytest.raises(IntegrityError, match=constraint):
        _add(db_session, _analysis(predio, user, **values))


def test_a_referenced_analysis_cannot_be_deleted(
    db_session: Session, predio: Predio, user: User
) -> None:
    existing = _completed(predio, user)
    _add(db_session, existing)
    _add(
        db_session,
        _analysis(
            predio,
            user,
            status=AnalysisStatus.DUPLICATE_SCENE,
            resolved_to_analysis_id=existing.id,
        ),
    )
    with pytest.raises(IntegrityError):
        db_session.execute(
            text("DELETE FROM analyses WHERE id = :id"), {"id": existing.id}
        )


# --- per-lote statistics -------------------------------------------------------------

_VALUES = {
    "mean": 0.71,
    "median": 0.73,
    "std_dev": 0.08,
    "min_value": 0.41,
    "max_value": 0.86,
    "percentile_10": 0.61,
    "percentile_90": 0.80,
}


def _stats(analysis: Analysis, lote: Lote, **overrides: Any) -> AnalysisLoteStats:
    values: dict[str, Any] = {
        "analysis_id": analysis.id,
        "lote_id": lote.id,
        "valid_pixels": 480,
        "total_pixels": 500,
        **_VALUES,
    }
    values.update(overrides)
    return AnalysisLoteStats(**values)


@pytest.fixture
def analysis(db_session: Session, predio: Predio, user: User) -> Analysis:
    row = _completed(predio, user)
    _add(db_session, row)
    return row


def test_measured_statistics_persist(
    db_session: Session, analysis: Analysis, lote: Lote
) -> None:
    _add(db_session, _stats(analysis, lote))


def test_an_excluded_lote_has_counts_and_no_values(
    db_session: Session, analysis: Analysis, lote: Lote
) -> None:
    no_values = dict.fromkeys(_VALUES)
    _add(
        db_session,
        _stats(
            analysis,
            lote,
            excluded_reason=LoteStatsExclusion.NOT_APPLICABLE_GREENHOUSE,
            **no_values,
        ),
    )


@pytest.mark.parametrize(
    "overrides",
    [
        # Excluded, yet carrying a number: exactly what must never be shown.
        {"excluded_reason": LoteStatsExclusion.INSUFFICIENT_PIXELS},
        # Measured, but with half its statistics missing.
        {"std_dev": None, "percentile_90": None},
    ],
)
def test_statistics_are_all_present_or_all_absent(
    db_session: Session, analysis: Analysis, lote: Lote, overrides: dict[str, Any]
) -> None:
    with pytest.raises(IntegrityError, match="measured_or_excluded"):
        _add(db_session, _stats(analysis, lote, **overrides))


def test_valid_pixels_cannot_exceed_the_total(
    db_session: Session, analysis: Analysis, lote: Lote
) -> None:
    with pytest.raises(IntegrityError, match="pixel_counts"):
        _add(db_session, _stats(analysis, lote, valid_pixels=501, total_pixels=500))


def test_one_row_per_lote_and_analysis(
    db_session: Session, analysis: Analysis, lote: Lote
) -> None:
    _add(db_session, _stats(analysis, lote))
    with pytest.raises(IntegrityError):
        _add(db_session, _stats(analysis, lote))


# --- anomalies ------------------------------------------------------------------------

ZONE = box(g.BASE_LON, g.BASE_LAT, g.BASE_LON + 0.001, g.BASE_LAT + 0.001)


def _anomaly(analysis: Analysis, lote: Lote | None, **overrides: Any) -> Anomaly:
    values: dict[str, Any] = {
        "analysis_id": analysis.id,
        "lote_id": lote.id if lote else None,
        "geometry": from_shape(ZONE, srid=4326),
        "centroid": from_shape(ZONE.centroid, srid=4326),
        "area_m2": 10_000.0,
        "area_ratio_of_lote": 0.04,
        "severity": AnomalySeverity.MEDIUM,
        "mean_zscore": -2.6,
        "mean_index_value": 0.38,
        "pixel_count": 100,
    }
    values.update(overrides)
    return Anomaly(**values)


def test_an_anomaly_persists_unreviewed(
    db_session: Session, analysis: Analysis, lote: Lote
) -> None:
    anomaly = _anomaly(analysis, lote)
    _add(db_session, anomaly)
    db_session.expire_all()
    stored = db_session.get(Anomaly, anomaly.id)
    assert stored is not None
    assert stored.review_status is None
    assert stored.created_at.tzinfo is not None


def test_a_complete_review_is_accepted(
    db_session: Session, analysis: Analysis, lote: Lote, user: User
) -> None:
    _add(
        db_session,
        _anomaly(
            analysis,
            lote,
            review_status=AnomalyReviewStatus.CONFIRMED,
            reviewed_at=datetime.now(UTC),
            reviewed_by_user_id=user.id,
            review_notes="Riego deficiente en la hilera norte",
        ),
    )


def test_a_review_without_its_reviewer_is_refused(
    db_session: Session, analysis: Analysis, lote: Lote
) -> None:
    with pytest.raises(IntegrityError, match="review_complete"):
        _add(
            db_session,
            _anomaly(
                analysis,
                lote,
                review_status=AnomalyReviewStatus.DISMISSED,
                reviewed_at=datetime.now(UTC),
            ),
        )


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"mean_zscore": 0.5}, "zscore_negative"),
        ({"area_ratio_of_lote": 1.5}, "area_ratio"),
        ({"area_m2": 0.0}, "area_positive"),
        ({"pixel_count": 0}, "pixel_count_positive"),
    ],
)
def test_inconsistent_anomalies_are_refused(
    db_session: Session,
    analysis: Analysis,
    lote: Lote,
    overrides: dict[str, Any],
    constraint: str,
) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        _add(db_session, _anomaly(analysis, lote, **overrides))


def test_an_anomaly_geometry_must_be_a_polygon(
    db_session: Session, analysis: Analysis, lote: Lote
) -> None:
    with pytest.raises(DBAPIError):
        _add(
            db_session,
            _anomaly(analysis, lote, geometry=from_shape(Point(-71.3, -34.6), srid=4326)),
        )


def test_the_anomaly_geometry_index_is_gist(db_session: Session) -> None:
    definition = db_session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_anomalies_geometry'")
    ).scalar_one()
    assert "USING gist" in definition


# --- relationships --------------------------------------------------------------------


def test_deleting_an_analysis_takes_its_results_with_it(
    db_session: Session, analysis: Analysis, lote: Lote
) -> None:
    _add(db_session, _stats(analysis, lote), _anomaly(analysis, lote))
    db_session.execute(text("DELETE FROM analyses WHERE id = :id"), {"id": analysis.id})
    assert db_session.execute(select(AnalysisLoteStats)).first() is None
    assert db_session.execute(select(Anomaly)).first() is None


def test_a_lote_with_results_cannot_be_physically_deleted(
    db_session: Session, analysis: Analysis, lote: Lote
) -> None:
    _add(db_session, _stats(analysis, lote))
    with pytest.raises(IntegrityError):
        db_session.execute(text("DELETE FROM lotes WHERE id = :id"), {"id": lote.id})


def test_the_runtime_role_can_manage_the_analysis_tables(app_session: Session) -> None:
    owner = User(
        email=f"runtime-{uuid.uuid4().hex[:8]}@example.cl",
        password_hash="$argon2id$placeholder",
        full_name="Runtime",
    )
    _add(app_session, owner)
    predio = create_predio(app_session, owner.id)
    lote = create_lote(app_session, predio)
    analysis = _completed(predio, owner)
    _add(app_session, analysis)
    stats = _stats(analysis, lote)
    anomaly = _anomaly(analysis, lote)
    _add(app_session, stats, anomaly)

    anomaly.review_notes = "updated by the runtime role"
    app_session.flush()
    app_session.execute(text("DELETE FROM anomalies WHERE id = :id"), {"id": anomaly.id})
    app_session.execute(
        text("DELETE FROM analysis_lote_stats WHERE id = :id"), {"id": stats.id}
    )
    app_session.execute(text("DELETE FROM analyses WHERE id = :id"), {"id": analysis.id})
    app_session.flush()
