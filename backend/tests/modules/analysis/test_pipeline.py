"""The analysis pipeline and the arq worker, end to end on synthetic scenes."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from shapely.geometry import shape
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.modules.analysis import worker
from app.modules.analysis.exceptions import AnalysisAlreadyActiveError
from app.modules.analysis.models import Analysis, AnalysisLoteStats, Anomaly
from app.modules.analysis.sentinel_client import SentinelQuotaExceededError
from app.modules.analysis.service import AnalysisService
from app.modules.audit import events
from app.modules.audit.models import AuditLog
from app.modules.predios.models import Lote, Predio
from app.modules.users.models import User
from app.shared.config import get_settings
from app.shared.enums import (
    AnalysisStatus,
    AnalysisType,
    AnomalyReviewStatus,
    LoteStatsExclusion,
    LoteType,
)
from app.shared.geo import contains_geojson
from app.shared.storage import LocalStorageBackend, analysis_artifact_key
from tests.fixtures import geometries as g
from tests.fixtures.predios import create_lote, create_predio
from tests.fixtures.sentinel.fake_client import (
    SCENE_ID,
    STRESSED_NIR,
    STRESSED_RED,
    FakeSentinelClient,
    Zone,
    corner,
)

HALF = g.STEP / 2


@pytest.fixture
def user(db_session: Session) -> User:
    person = User(
        email=f"pipeline-{uuid.uuid4().hex[:8]}@example.cl",
        password_hash="$argon2id$placeholder",
        full_name="Pipeline",
    )
    db_session.add(person)
    db_session.flush()
    return person


@pytest.fixture
def predio(db_session: Session, user: User) -> Predio:
    return create_predio(db_session, user.id)


@pytest.fixture
def cuartel(db_session: Session, predio: Predio) -> Lote:
    return create_lote(db_session, predio, name="Cuartel 1")


@pytest.fixture
def greenhouse(db_session: Session, predio: Predio) -> Lote:
    return create_lote(
        db_session,
        predio,
        geojson=g.square(lon=g.BASE_LON + HALF, size=HALF),
        name="Invernadero 1",
        lote_type=LoteType.INVERNADERO,
    )


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorageBackend:
    return LocalStorageBackend(tmp_path / "rasters")


@pytest.fixture
def service(db_session: Session) -> AnalysisService:
    return AnalysisService(db_session, get_settings())


def _pending(
    service: AnalysisService, predio: Predio, user: User, *, force: bool = False
) -> Analysis:
    return service.create_pending(
        predio_id=predio.id,
        analysis_type=AnalysisType.NDVI,
        date_from=date(2026, 9, 1),
        date_to=date(2026, 9, 30),
        requested_by=user.id,
        force=force,
    )


def _events(session: Session, analysis: Analysis) -> list[AuditLog]:
    statement = select(AuditLog).where(AuditLog.target_resource_id == analysis.id)
    return list(session.execute(statement).scalars())


def _stressed_corner() -> Zone:
    """Stress the south-west fifth of the cuartel (the predio's SW quarter)."""
    cuartel_shape = shape(g.square(size=HALF))
    return Zone(corner(cuartel_shape, 0.2), red=STRESSED_RED, nir=STRESSED_NIR)


# --- a successful run --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_run_completes_with_statistics_anomalies_and_rasters(
    db_session: Session,
    service: AnalysisService,
    predio: Predio,
    user: User,
    cuartel: Lote,
    greenhouse: Lote,
    storage: LocalStorageBackend,
) -> None:
    analysis = _pending(service, predio, user)
    client = FakeSentinelClient(zones=[_stressed_corner()])

    status = await service.run(analysis.id, client, storage)

    assert status is AnalysisStatus.COMPLETED
    db_session.expire_all()
    stored = db_session.get(Analysis, analysis.id)
    assert stored is not None
    assert (stored.scene_id, stored.scene_date) == (SCENE_ID, date(2026, 9, 15))
    assert stored.cloud_coverage == pytest.approx(0.12)
    assert stored.processing_units_spent == pytest.approx(3.5)
    assert stored.valid_pixels_ratio == pytest.approx(1.0)
    assert stored.started_at is not None and stored.completed_at is not None

    stats = {
        row.lote_id: row
        for row in db_session.execute(
            select(AnalysisLoteStats).where(AnalysisLoteStats.analysis_id == analysis.id)
        ).scalars()
    }
    assert stats[cuartel.id].mean is not None
    assert (
        stats[greenhouse.id].excluded_reason
        is LoteStatsExclusion.NOT_APPLICABLE_GREENHOUSE
    )
    assert stats[greenhouse.id].mean is None

    anomalies = list(
        db_session.execute(
            select(Anomaly).where(Anomaly.analysis_id == analysis.id)
        ).scalars()
    )
    assert [a.lote_id for a in anomalies] == [cuartel.id]

    for artifact in ("ndvi.tif", "preview.png", "metadata.json"):
        assert await storage.exists(
            analysis_artifact_key(predio.id, analysis.id, artifact)
        )
    assert stored.raster_key and stored.raster_key.endswith("/ndvi.tif")


@pytest.mark.asyncio
async def test_a_run_leaves_an_audit_trail_without_rasters_or_geometries(
    db_session: Session,
    service: AnalysisService,
    predio: Predio,
    user: User,
    cuartel: Lote,
    storage: LocalStorageBackend,
) -> None:
    analysis = _pending(service, predio, user)
    await service.run(
        analysis.id, FakeSentinelClient(zones=[_stressed_corner()]), storage
    )

    trail = {entry.event_type: entry for entry in _events(db_session, analysis)}
    assert set(trail) >= {
        events.ANALYSIS_REQUESTED,
        events.ANALYSIS_STARTED,
        events.ANALYSIS_COMPLETED,
        events.ANOMALY_DETECTED,
    }
    completed = trail[events.ANALYSIS_COMPLETED].details
    assert completed["processing_units_spent"] == pytest.approx(3.5)
    assert completed["reflectance_percentiles"]["B08"]["p50"] == pytest.approx(0.45)
    assert completed["anomalies_by_severity"]
    for entry in trail.values():
        assert not contains_geojson(entry.details)


# --- failure paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unexpected_exception_fails_the_analysis_with_detail(
    db_session: Session,
    service: AnalysisService,
    predio: Predio,
    user: User,
    storage: LocalStorageBackend,
) -> None:
    analysis = _pending(service, predio, user)
    client = FakeSentinelClient(fetch_error=RuntimeError("disk on fire"))

    status = await service.run(analysis.id, client, storage)

    assert status is AnalysisStatus.FAILED
    db_session.expire_all()
    stored = db_session.get(Analysis, analysis.id)
    assert stored is not None
    assert stored.error_code == "INTERNAL_ERROR"
    assert stored.error_detail == {"error": "RuntimeError", "message": "disk on fire"}
    assert stored.completed_at is not None
    failed = [
        e for e in _events(db_session, analysis) if e.event_type == events.ANALYSIS_FAILED
    ]
    assert len(failed) == 1


@pytest.mark.asyncio
async def test_a_domain_error_is_recorded_with_its_own_code(
    db_session: Session,
    service: AnalysisService,
    predio: Predio,
    user: User,
    storage: LocalStorageBackend,
) -> None:
    analysis = _pending(service, predio, user)
    client = FakeSentinelClient(fetch_error=SentinelQuotaExceededError(30_001.0, 30_000))

    await service.run(analysis.id, client, storage)

    db_session.expire_all()
    stored = db_session.get(Analysis, analysis.id)
    assert stored is not None and stored.error_code == "QUOTA_EXCEEDED"
    assert stored.error_detail == {
        "error": "SentinelQuotaExceededError",
        "spent_pu": 30_001.0,
        "budget_pu": 30_000,
    }


@pytest.mark.asyncio
async def test_a_cloudy_range_ends_in_no_suitable_scene_with_its_reasons(
    db_session: Session,
    service: AnalysisService,
    predio: Predio,
    user: User,
    storage: LocalStorageBackend,
) -> None:
    analysis = _pending(service, predio, user)
    status = await service.run(analysis.id, FakeSentinelClient(cloudy=True), storage)

    assert status is AnalysisStatus.NO_SUITABLE_SCENE
    db_session.expire_all()
    stored = db_session.get(Analysis, analysis.id)
    assert stored is not None
    assert stored.error_code is None  # not a system error
    assert stored.error_detail["scenes_evaluated"] == 2  # type: ignore[index]
    assert stored.error_detail["best_cloud_fraction"] == pytest.approx(0.47)  # type: ignore[index]


@pytest.mark.asyncio
async def test_a_scene_mostly_masked_after_download_is_no_suitable_scene(
    db_session: Session,
    service: AnalysisService,
    predio: Predio,
    user: User,
    storage: LocalStorageBackend,
) -> None:
    analysis = _pending(service, predio, user)
    predio_shape = shape(g.SQUARE)
    clouds = Zone(corner(predio_shape, 0.8), scl=9)  # 64% of the predio
    status = await service.run(analysis.id, FakeSentinelClient(zones=[clouds]), storage)

    assert status is AnalysisStatus.NO_SUITABLE_SCENE
    db_session.expire_all()
    stored = db_session.get(Analysis, analysis.id)
    assert stored is not None
    assert stored.error_detail["valid_pixels_ratio"] < 0.60  # type: ignore[index]
    assert stored.processing_units_spent == pytest.approx(3.5)


# --- idempotency and concurrency ---------------------------------------------


@pytest.mark.asyncio
async def test_a_retry_on_a_completed_analysis_does_nothing(
    service: AnalysisService,
    predio: Predio,
    user: User,
    storage: LocalStorageBackend,
) -> None:
    analysis = _pending(service, predio, user)
    client = FakeSentinelClient()
    await service.run(analysis.id, client, storage)
    calls_after_first_run = list(client.calls)

    assert await service.run(analysis.id, client, storage) is AnalysisStatus.COMPLETED
    assert client.calls == calls_after_first_run  # no second download, no quota


# --- duplicates and forced recomputes ------------------------------------------


@pytest.mark.asyncio
async def test_an_already_analysed_scene_resolves_to_the_existing_analysis(
    db_session: Session,
    service: AnalysisService,
    predio: Predio,
    user: User,
    storage: LocalStorageBackend,
) -> None:
    first = _pending(service, predio, user)
    await service.run(first.id, FakeSentinelClient(), storage)
    second = _pending(service, predio, user)
    client = FakeSentinelClient()

    status = await service.run(second.id, client, storage)

    # Not a failure: the result exists, the request points at it.
    assert status is AnalysisStatus.DUPLICATE_SCENE
    db_session.expire_all()
    stored = db_session.get(Analysis, second.id)
    assert stored is not None
    assert stored.resolved_to_analysis_id == first.id
    assert stored.error_code is None
    assert stored.scene_id == SCENE_ID
    assert stored.raster_key is None
    event_types = [event.event_type for event in _events(db_session, second)]
    assert events.ANALYSIS_RESOLVED_TO_EXISTING in event_types
    assert events.ANALYSIS_FAILED not in event_types


@pytest.mark.asyncio
async def test_resolving_a_duplicate_downloads_no_band(
    db_session: Session,
    service: AnalysisService,
    predio: Predio,
    user: User,
    storage: LocalStorageBackend,
) -> None:
    first = _pending(service, predio, user)
    await service.run(first.id, FakeSentinelClient(), storage)
    second = _pending(service, predio, user)
    client = FakeSentinelClient()

    await service.run(second.id, client, storage)

    assert client.calls == ["select_scene"]  # stopped before fetch_bands
    db_session.expire_all()
    stored = db_session.get(Analysis, second.id)
    assert stored is not None
    # Only scene selection was spent: zero PU went into downloading bands.
    assert stored.processing_units_spent == pytest.approx(client.selection_pu)
    assert client.processing_units_spent - client.selection_pu == pytest.approx(0.0)


def _review_first_anomaly(session: Session, analysis: Analysis, user: User) -> Anomaly:
    reviewer_id = user.id  # read before touching the row: no half-review autoflush
    anomaly = session.execute(
        select(Anomaly).where(Anomaly.analysis_id == analysis.id).limit(1)
    ).scalar_one()
    anomaly.review_status = AnomalyReviewStatus.CONFIRMED
    anomaly.review_notes = "Riego tapado en la hilera 12"
    anomaly.reviewed_by_user_id = reviewer_id
    anomaly.reviewed_at = datetime.now(UTC)
    session.commit()
    return anomaly


@pytest.mark.asyncio
async def test_a_forced_recompute_supersedes_the_previous_result_and_keeps_it(
    db_session: Session,
    service: AnalysisService,
    predio: Predio,
    user: User,
    cuartel: Lote,
    storage: LocalStorageBackend,
) -> None:
    first = _pending(service, predio, user)
    await service.run(first.id, FakeSentinelClient(zones=[_stressed_corner()]), storage)
    _review_first_anomaly(db_session, first, user)
    forced = _pending(service, predio, user, force=True)
    client = FakeSentinelClient(zones=[_stressed_corner()])

    status = await service.run(forced.id, client, storage)

    assert status is AnalysisStatus.COMPLETED
    assert "fetch_bands" in client.calls  # force does download again
    db_session.expire_all()
    old = db_session.get(Analysis, first.id)
    new = db_session.get(Analysis, forced.id)
    assert old is not None and new is not None
    # Both COMPLETED on the same scene; only the new one is vigente, so the
    # one-vigente-per-scene index holds.
    assert old.status is AnalysisStatus.COMPLETED
    assert old.superseded_by_analysis_id == new.id
    assert new.superseded_by_analysis_id is None
    assert old.scene_id == new.scene_id == SCENE_ID
    assert old.raster_key is not None and await storage.exists(old.raster_key)


@pytest.mark.asyncio
async def test_the_superseded_analysis_keeps_its_anomalies_and_reviews(
    db_session: Session,
    service: AnalysisService,
    predio: Predio,
    user: User,
    cuartel: Lote,
    storage: LocalStorageBackend,
) -> None:
    first = _pending(service, predio, user)
    await service.run(first.id, FakeSentinelClient(zones=[_stressed_corner()]), storage)
    reviewed = _review_first_anomaly(db_session, first, user)
    anomalies_before = service.anomalies(predio.id, first.id)
    forced = _pending(service, predio, user, force=True)

    await service.run(forced.id, FakeSentinelClient(zones=[_stressed_corner()]), storage)

    db_session.expire_all()
    kept = service.anomalies(predio.id, first.id)
    assert {a.id for a in kept} == {a.id for a in anomalies_before}
    still_reviewed = db_session.get(Anomaly, reviewed.id)
    assert still_reviewed is not None
    assert still_reviewed.review_status is AnomalyReviewStatus.CONFIRMED
    assert still_reviewed.review_notes == "Riego tapado en la hilera 12"
    # The new analysis starts with its own, unreviewed anomalies.
    fresh = service.anomalies(predio.id, forced.id)
    assert fresh and all(a.review_status is None for a in fresh)

    recompute = [
        e
        for e in _events(db_session, forced)
        if e.event_type == events.ANALYSIS_FORCED_RECOMPUTE
    ]
    assert len(recompute) == 1
    assert recompute[0].severity.value == "WARNING"
    assert recompute[0].details == {
        "superseded_analysis_id": str(first.id),
        "scene_id": SCENE_ID,
        "reviewed_anomalies": 1,
    }


@pytest.mark.asyncio
async def test_after_a_recompute_a_plain_request_resolves_to_the_new_result(
    db_session: Session,
    service: AnalysisService,
    predio: Predio,
    user: User,
    storage: LocalStorageBackend,
) -> None:
    first = _pending(service, predio, user)
    await service.run(first.id, FakeSentinelClient(), storage)
    forced = _pending(service, predio, user, force=True)
    await service.run(forced.id, FakeSentinelClient(), storage)
    plain = _pending(service, predio, user)

    await service.run(plain.id, FakeSentinelClient(), storage)

    db_session.expire_all()
    stored = db_session.get(Analysis, plain.id)
    assert stored is not None
    assert stored.status is AnalysisStatus.DUPLICATE_SCENE
    assert stored.resolved_to_analysis_id == forced.id


@pytest.mark.asyncio
async def test_force_on_a_new_scene_completes_without_superseding(
    db_session: Session,
    service: AnalysisService,
    predio: Predio,
    user: User,
    storage: LocalStorageBackend,
) -> None:
    forced = _pending(service, predio, user, force=True)

    await service.run(forced.id, FakeSentinelClient(), storage)

    event_types = [e.event_type for e in _events(db_session, forced)]
    assert events.ANALYSIS_COMPLETED in event_types
    assert events.ANALYSIS_FORCED_RECOMPUTE not in event_types


def test_recompute_risk_is_none_without_a_previous_result(
    db_session: Session, service: AnalysisService, predio: Predio, user: User
) -> None:
    assert (
        service.recompute_risk(
            predio.id, AnalysisType.NDVI, date(2026, 9, 1), date(2026, 9, 30)
        )
        is None
    )


@pytest.mark.asyncio
async def test_recompute_risk_points_at_the_analysis_with_reviews(
    db_session: Session,
    service: AnalysisService,
    predio: Predio,
    user: User,
    cuartel: Lote,
    storage: LocalStorageBackend,
) -> None:
    first = _pending(service, predio, user)
    await service.run(first.id, FakeSentinelClient(zones=[_stressed_corner()]), storage)
    _review_first_anomaly(db_session, first, user)

    risk = service.recompute_risk(
        predio.id, AnalysisType.NDVI, date(2026, 9, 1), date(2026, 9, 30)
    )
    outside = service.recompute_risk(
        predio.id, AnalysisType.NDVI, date(2026, 8, 1), date(2026, 8, 31)
    )

    assert risk is not None
    assert (risk.analysis_id, risk.reviewed_anomalies) == (first.id, 1)
    assert outside is None  # the scene (09-15) is not in August


# --- one analysis in flight --------------------------------------------------


def test_a_second_request_in_flight_learns_which_analysis_is_running(
    service: AnalysisService, predio: Predio, user: User
) -> None:
    first = _pending(service, predio, user)

    with pytest.raises(AnalysisAlreadyActiveError) as raised:
        _pending(service, predio, user)

    assert raised.value.active_analysis_id == first.id
    assert raised.value.as_dict()["analysis_id"] == str(first.id)


@pytest.mark.asyncio
async def test_the_in_flight_slot_is_released_when_the_analysis_finishes(
    service: AnalysisService,
    predio: Predio,
    user: User,
    storage: LocalStorageBackend,
) -> None:
    first = _pending(service, predio, user)
    with pytest.raises(AnalysisAlreadyActiveError):
        _pending(service, predio, user)

    await service.run(first.id, FakeSentinelClient(), storage)

    second = _pending(service, predio, user)
    assert second.status is AnalysisStatus.PENDING


def test_two_analyses_of_a_type_are_never_queued_for_one_predio(
    service: AnalysisService, predio: Predio, user: User
) -> None:
    _pending(service, predio, user)
    with pytest.raises(AnalysisAlreadyActiveError):
        _pending(service, predio, user)


# --- zombies -----------------------------------------------------------------


def test_stale_running_analyses_are_failed_with_timeout(
    db_session: Session, service: AnalysisService, predio: Predio, user: User
) -> None:
    stale = _pending(service, predio, user)
    stale.status = AnalysisStatus.RUNNING
    stale.started_at = datetime.now(UTC) - timedelta(hours=2)
    db_session.flush()

    assert service.expire_stale() == 1
    db_session.expire_all()
    stored = db_session.get(Analysis, stale.id)
    assert stored is not None
    assert (stored.status, stored.error_code) == (AnalysisStatus.FAILED, "TIMEOUT")


def test_a_pending_analysis_whose_job_never_ran_expires(
    db_session: Session, service: AnalysisService, predio: Predio, user: User
) -> None:
    lost = _pending(service, predio, user)
    later = datetime.now(UTC) + timedelta(
        seconds=get_settings().analysis_pending_timeout_s + 60
    )

    assert service.expire_stale(now=later) == 1
    db_session.expire_all()
    stored = db_session.get(Analysis, lost.id)
    assert stored is not None
    assert (stored.status, stored.error_code) == (AnalysisStatus.FAILED, "QUEUE_TIMEOUT")
    # The predio's in-flight slot is free again.
    assert _pending(service, predio, user).status is AnalysisStatus.PENDING


def test_a_pending_analysis_waiting_in_a_busy_queue_is_left_alone(
    service: AnalysisService, predio: Predio, user: User
) -> None:
    _pending(service, predio, user)

    assert service.expire_stale() == 0


def test_a_recent_running_analysis_is_left_alone(
    db_session: Session, service: AnalysisService, predio: Predio, user: User
) -> None:
    recent = _pending(service, predio, user)
    recent.status = AnalysisStatus.RUNNING
    recent.started_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.flush()

    assert service.expire_stale() == 0
    assert db_session.get(Analysis, recent.id).status is AnalysisStatus.RUNNING  # type: ignore[union-attr]


# --- the arq worker ----------------------------------------------------------


@pytest.fixture
def worker_ctx(db_session: Session, storage: LocalStorageBackend) -> dict[str, Any]:
    """Return a worker context whose sessions join the test's transaction."""
    factory = sessionmaker(
        bind=db_session.connection(), join_transaction_mode="create_savepoint"
    )
    return {
        "settings": get_settings(),
        "storage": storage,
        "session_factory": factory,
        "redis": None,
    }


@pytest.mark.asyncio
async def test_the_worker_fails_a_job_when_analysis_is_unavailable(
    db_session: Session,
    service: AnalysisService,
    predio: Predio,
    user: User,
    worker_ctx: dict[str, Any],
) -> None:
    # The suite never has Sentinel credentials (see conftest.py).
    analysis = _pending(service, predio, user)
    assert await worker.run_analysis(worker_ctx, str(analysis.id)) is None

    db_session.expire_all()
    stored = db_session.get(Analysis, analysis.id)
    assert stored is not None
    assert (stored.status, stored.error_code) == (
        AnalysisStatus.FAILED,
        "ANALYSIS_UNAVAILABLE",
    )


@pytest.mark.asyncio
async def test_the_periodic_task_expires_zombies(
    db_session: Session,
    service: AnalysisService,
    predio: Predio,
    user: User,
    worker_ctx: dict[str, Any],
) -> None:
    stale = _pending(service, predio, user)
    stale.status = AnalysisStatus.RUNNING
    stale.started_at = datetime.now(UTC) - timedelta(hours=2)
    db_session.commit()

    assert await worker.expire_stale_analyses(worker_ctx) == 1


def test_the_worker_is_configured_for_bounded_memory() -> None:
    settings = worker.WorkerSettings
    assert settings.max_jobs == 2
    assert worker.run_analysis in settings.functions
    assert len(settings.cron_jobs) == 1
    assert settings.job_timeout > get_settings().analysis_job_timeout_s
    assert worker.job_id_for(uuid.UUID(int=1)) == f"analysis:{uuid.UUID(int=1)}"
