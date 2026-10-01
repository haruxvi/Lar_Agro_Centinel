"""Business logic of satellite analysis: request, run, expire.

Runs in the arq worker (see worker.py), never inside an HTTP request: a
Sentinel Hub round trip takes 10-60 s. The service knows nothing about the
web layer or about arq; both call into it.

Status transitions: PENDING -> RUNNING -> COMPLETED | FAILED |
NO_SUITABLE_SCENE | DUPLICATE_SCENE. NO_SUITABLE_SCENE is a cloudy sky, not a
system error. DUPLICATE_SCENE is not an error either: the chosen scene already
had a result, the request resolves to it, and no band is downloaded. With
``force`` the scene is recomputed and the previous result is superseded, never
deleted: its anomalies and their reviews stay on it (KL-007).
Whatever goes wrong, a run never leaves its analysis in RUNNING: unexpected
errors end in FAILED with a usable detail, and expire_stale() catches the
runs whose worker died.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from geoalchemy2.shape import from_shape
from shapely.geometry.base import BaseGeometry
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.analysis.anomaly import DetectedAnomaly, SpatialZScore
from app.modules.analysis.exceptions import (
    AnalysisAlreadyActiveError,
    AnalysisError,
    AnalysisNotFoundError,
    AnomalyNotFoundError,
    InvalidDateRangeError,
    PredioTooLargeError,
)
from app.modules.analysis.models import Analysis, AnalysisLoteStats, Anomaly
from app.modules.analysis.ndvi import (
    InsufficientValidPixelsError,
    LoteNdviStats,
    NdviResult,
    ReflectanceScaleError,
    lote_statistics,
    process_scene,
    render_preview,
    write_geotiff,
)
from app.modules.analysis.repository import (
    AnalysisFilters,
    AnalysisRepository,
    LoteHistoryPoint,
)
from app.modules.analysis.sentinel_client import (
    NoSuitableSceneFoundError,
    PuUsage,
    SceneSelection,
    SentinelAuthError,
    SentinelCircuitOpenError,
    SentinelQuotaExceededError,
    SentinelRequestError,
    SentinelResponse,
)
from app.modules.audit import events
from app.modules.audit.service import AuditService
from app.modules.predios.exceptions import LoteNotFoundError, PredioNotFoundError
from app.modules.predios.models import Lote
from app.modules.predios.repository import LoteRepository, PredioRepository
from app.modules.predios.service import Actor
from app.shared.config import Settings, get_settings
from app.shared.enums import (
    AnalysisStatus,
    AnalysisType,
    AnomalyReviewStatus,
    AnomalySeverity,
    AuditOutcome,
)
from app.shared.exceptions import DomainError
from app.shared.geo import contains_geojson, wkb_to_geometry
from app.shared.pagination import Page, PageParams
from app.shared.storage import StorageBackend, analysis_artifact_key

BANDS = ["B04", "B08", "SCL"]

# What each failure is called in analyses.error_code.
_ERROR_CODES: dict[type[Exception], str] = {
    SentinelQuotaExceededError: "QUOTA_EXCEEDED",
    SentinelCircuitOpenError: "SENTINEL_UNAVAILABLE",
    SentinelAuthError: "SENTINEL_AUTH",
    SentinelRequestError: "SENTINEL_ERROR",
    ReflectanceScaleError: "REFLECTANCE_SCALE",
}
_DETAIL_MESSAGE_LIMIT = 300
_ACTIVE_INDEX = "uq_analyses_one_active_per_type"


class SentinelGateway(Protocol):
    """What the pipeline needs from Sentinel Hub (the real client or a fake)."""

    async def select_scene(
        self, geometry: BaseGeometry, date_from: date, date_to: date
    ) -> SceneSelection:
        """Pick the most recent acquisition clear enough over the predio."""
        ...

    async def fetch_bands(
        self, geometry: BaseGeometry, date_from: date, date_to: date, bands: list[str]
    ) -> SentinelResponse:
        """Download the bands for the chosen date."""
        ...

    @property
    def processing_units_spent(self) -> float:
        """Return the PU this gateway has spent."""
        ...


@dataclass(frozen=True)
class RecomputeRisk:
    """What a forced recompute would leave behind on the analysis it replaces."""

    analysis_id: uuid.UUID
    reviewed_anomalies: int


@dataclass(frozen=True)
class AnalysisDetail:
    """An analysis with the results a detail view shows."""

    analysis: Analysis
    lote_stats: list[AnalysisLoteStats]
    anomaly_counts: dict[AnomalySeverity, int]


@dataclass(frozen=True)
class _LoteResult:
    lote: Lote
    stats: LoteNdviStats
    anomalies: list[DetectedAnomaly]


def _error_code(exc: Exception) -> str:
    for kind, code in _ERROR_CODES.items():
        if isinstance(exc, kind):
            return code
    return "INTERNAL_ERROR"


def _violated_constraint(exc: IntegrityError) -> str | None:
    diagnostics = getattr(exc.orig, "diag", None)
    name = getattr(diagnostics, "constraint_name", None)
    return name if isinstance(name, str) else None


def _error_detail(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, DomainError):
        return exc.as_dict()
    # Unexpected: the type and a bounded message are what an operator needs.
    return {"error": type(exc).__name__, "message": str(exc)[:_DETAIL_MESSAGE_LIMIT]}


class AnalysisService:
    """Requests, runs and expires analyses."""

    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        """Bind the service to a session and the settings."""
        self._session = session
        self._settings = settings or get_settings()
        self._analyses = AnalysisRepository(session)
        self._audit = AuditService(session)

    # --- audit ----------------------------------------------------------------------

    def _record(
        self,
        event_type: str,
        analysis: Analysis,
        details: dict[str, Any],
        outcome: AuditOutcome = AuditOutcome.SUCCESS,
        actor: Actor | None = None,
    ) -> None:
        """Append a catalogued event; never a raster, never a geometry.

        Without an actor the event is attributed to whoever requested the
        analysis (the worker acts on their behalf).
        """
        spec = events.ANALYSIS_EVENTS[event_type]
        if contains_geojson(details):
            raise ValueError(f"{event_type}: audit details must not carry a geometry")
        self._audit.record(
            event_category=spec.category,
            event_type=event_type,
            severity=spec.severity,
            action=event_type.lower().replace("_", " "),
            outcome=outcome,
            actor_user_id=actor.user_id if actor else analysis.requested_by_user_id,
            actor_role=actor.role if actor else None,
            actor_ip=actor.ip if actor else None,
            actor_user_agent=actor.user_agent if actor else None,
            target_resource_type="analysis",
            target_resource_id=analysis.id,
            predio_id=analysis.predio_id,
            details=details,
        )

    # --- request --------------------------------------------------------------------

    def validate_request(
        self,
        predio_id: uuid.UUID,
        date_from: date | None,
        date_to: date | None,
        today: date,
    ) -> tuple[date, date]:
        """Return the range to analyse, or refuse a request that cannot work.

        Missing ends default to the last ``analysis_default_date_range_days``.
        Refused here, before anything is queued: a range in the future, a
        reversed or too long range, a predio larger than one analysis covers.
        """
        predio = PredioRepository(self._session).get_by_id(predio_id)
        if predio is None:
            raise PredioNotFoundError
        if predio.area_m2 > self._settings.analysis_max_predio_area_m2:
            raise PredioTooLargeError(
                predio.area_m2, self._settings.analysis_max_predio_area_m2
            )
        end = date_to or today
        start = date_from or end - timedelta(
            days=self._settings.analysis_default_date_range_days
        )
        if end > today:
            raise InvalidDateRangeError("La fecha final no puede estar en el futuro.")
        if start > end:
            raise InvalidDateRangeError(
                "La fecha inicial debe ser anterior o igual a la final."
            )
        span = (end - start).days
        if span > self._settings.analysis_max_date_range_days:
            raise InvalidDateRangeError(
                f"El rango no puede superar "
                f"{self._settings.analysis_max_date_range_days} días."
            )
        return start, end

    def check_budget(self, usage: PuUsage) -> None:
        """Refuse a request when this month's Processing Units are spent."""
        if usage.spent >= usage.budget:
            raise SentinelQuotaExceededError(usage.spent, usage.budget)

    def create_pending(
        self,
        *,
        predio_id: uuid.UUID,
        analysis_type: AnalysisType,
        date_from: date,
        date_to: date,
        requested_by: uuid.UUID,
        force: bool = False,
        actor: Actor | None = None,
    ) -> Analysis:
        """Create a PENDING analysis. Whether its scene is new is not known yet.

        Duplicates are resolved in the worker, once the scene is chosen. The
        only refusal here is a second analysis of the type in flight, and it
        comes from the partial unique index: two concurrent requests cannot
        both get through, and the loser learns which one is running.
        """
        analysis = Analysis(
            predio_id=predio_id,
            analysis_type=analysis_type,
            status=AnalysisStatus.PENDING,
            requested_date_from=date_from,
            requested_date_to=date_to,
            requested_by_user_id=requested_by,
            force=force,
        )
        try:
            with self._session.begin_nested():
                self._analyses.add(analysis)
        except IntegrityError as exc:
            if _violated_constraint(exc) != _ACTIVE_INDEX:
                raise
            active = self._analyses.find_active(predio_id, analysis_type)
            raise AnalysisAlreadyActiveError(active.id if active else None) from exc
        self._record(
            events.ANALYSIS_REQUESTED,
            analysis,
            {
                "analysis_type": analysis_type.value,
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "force": force,
            },
            actor=actor,
        )
        self._session.commit()
        return analysis

    def recompute_risk(
        self,
        predio_id: uuid.UUID,
        analysis_type: AnalysisType,
        date_from: date,
        date_to: date,
    ) -> RecomputeRisk | None:
        """Return what a forced recompute over the range would likely displace.

        The scene is chosen later, in the worker, so this is the newest
        vigente analysis whose scene falls in the range: the one scene
        selection would pick again unless a newer clear scene appeared.
        """
        existing = self._analyses.latest_current_in_range(
            predio_id, analysis_type, date_from, date_to
        )
        if existing is None:
            return None
        return RecomputeRisk(
            existing.id, self._analyses.count_reviewed_anomalies(existing.id)
        )

    # --- run ------------------------------------------------------------------------

    async def run(
        self, analysis_id: uuid.UUID, client: SentinelGateway, storage: StorageBackend
    ) -> AnalysisStatus | None:
        """Run one analysis end to end. Safe to call again for the same id.

        Only a PENDING analysis is run. A retried job that finds it COMPLETED
        (or in any other state) returns at once: recomputing costs quota.
        """
        analysis = self._analyses.lock(analysis_id)
        if analysis is None or analysis.status is not AnalysisStatus.PENDING:
            status = analysis.status if analysis else None
            self._session.rollback()
            return status

        analysis.status = AnalysisStatus.RUNNING
        analysis.started_at = datetime.now(UTC)
        self._record(events.ANALYSIS_STARTED, analysis, {})
        self._session.commit()
        clock = time.monotonic()

        try:
            await self._execute(analysis, client, storage, clock)
        except NoSuitableSceneFoundError as exc:
            self._finish_without_scene(analysis, exc.as_dict(), client, clock)
        except Exception as exc:  # noqa: BLE001 - nothing may leave it RUNNING
            self._session.rollback()
            self._fail(analysis_id, _error_code(exc), _error_detail(exc), client, clock)
        refreshed = self._analyses.get(analysis_id)
        return refreshed.status if refreshed else None

    async def _execute(
        self,
        analysis: Analysis,
        client: SentinelGateway,
        storage: StorageBackend,
        clock: float,
    ) -> None:
        predio = PredioRepository(self._session).get_by_id(analysis.predio_id)
        if predio is None:
            raise AnalysisError("the predio no longer exists")
        geometry = wkb_to_geometry(predio.geometry)
        lotes = LoteRepository(self._session).list_for_predio(predio.id)

        selection = await client.select_scene(
            geometry, analysis.requested_date_from, analysis.requested_date_to
        )
        existing = self._analyses.find_current_completed(
            predio.id, analysis.analysis_type, selection.scene_id
        )
        if existing is not None and not analysis.force:
            # Before downloading anything: a duplicate must not cost quota.
            self._resolve_to_existing(analysis, existing, selection, client, clock)
            return
        analysis.scene_id = selection.scene_id
        analysis.scene_date = selection.scene_date
        analysis.cloud_coverage = selection.cloud_coverage

        response = await client.fetch_bands(
            geometry, selection.scene_date, selection.scene_date, BANDS
        )
        try:
            result = await asyncio.to_thread(
                process_scene, response.data, geometry, self._settings
            )
        except InsufficientValidPixelsError as exc:
            detail = {
                **exc.as_dict(),
                "scene_date": selection.scene_date.isoformat(),
                "cloud_coverage": round(selection.cloud_coverage, 4),
                "evaluated": [o.as_dict() for o in selection.evaluated],
            }
            self._finish_without_scene(analysis, detail, client, clock)
            return

        per_lote = await asyncio.to_thread(self._measure_lotes, result, lotes)
        raster_key, preview_key = await self._store(
            storage, analysis, result, selection, client
        )
        self._persist_results(analysis, per_lote)
        self._complete(
            analysis, result, selection, per_lote, raster_key, preview_key, client, clock
        )

    def _resolve_to_existing(
        self,
        analysis: Analysis,
        existing: Analysis,
        selection: SceneSelection,
        client: SentinelGateway,
        clock: float,
    ) -> None:
        analysis.status = AnalysisStatus.DUPLICATE_SCENE
        analysis.resolved_to_analysis_id = existing.id
        analysis.scene_id = selection.scene_id
        analysis.scene_date = selection.scene_date
        analysis.cloud_coverage = selection.cloud_coverage
        analysis.completed_at = datetime.now(UTC)
        analysis.processing_units_spent = client.processing_units_spent
        self._record(
            events.ANALYSIS_RESOLVED_TO_EXISTING,
            analysis,
            {
                "resolved_to_analysis_id": str(existing.id),
                "scene_id": selection.scene_id,
                "scene_date": selection.scene_date.isoformat(),
                "processing_units_spent": round(client.processing_units_spent, 4),
                "duration_s": round(time.monotonic() - clock, 2),
            },
        )
        self._session.commit()

    def _supersede_previous(self, analysis: Analysis, scene_id: str) -> None:
        """Mark the vigente result of the scene as replaced by ``analysis``.

        Looked up again, locked, at completion time rather than reused from
        the start of the run: what counts is the result vigente when this one
        takes its place. Flushed before ``analysis`` turns COMPLETED, so the
        one-vigente-per-scene index never sees two at once.
        """
        previous = self._analyses.find_current_completed(
            analysis.predio_id, analysis.analysis_type, scene_id, lock=True
        )
        if previous is None:
            return
        reviewed = self._analyses.count_reviewed_anomalies(previous.id)
        previous.superseded_by_analysis_id = analysis.id
        self._session.flush()
        self._record(
            events.ANALYSIS_FORCED_RECOMPUTE,
            analysis,
            {
                "superseded_analysis_id": str(previous.id),
                "scene_id": scene_id,
                "reviewed_anomalies": reviewed,
            },
        )

    def _measure_lotes(self, result: NdviResult, lotes: list[Lote]) -> list[_LoteResult]:
        method = SpatialZScore()
        measured: list[_LoteResult] = []
        for lote in lotes:
            lote_geometry = wkb_to_geometry(lote.geometry)
            stats = lote_statistics(result, lote_geometry, lote.lote_type, self._settings)
            anomalies = method.detect(result, lote_geometry, stats, self._settings)
            measured.append(_LoteResult(lote, stats, anomalies))
        return measured

    async def _store(
        self,
        storage: StorageBackend,
        analysis: Analysis,
        result: NdviResult,
        selection: SceneSelection,
        client: SentinelGateway,
    ) -> tuple[str, str]:
        raster_key = analysis_artifact_key(analysis.predio_id, analysis.id, "ndvi.tif")
        preview_key = analysis_artifact_key(
            analysis.predio_id, analysis.id, "preview.png"
        )
        metadata_key = analysis_artifact_key(
            analysis.predio_id, analysis.id, "metadata.json"
        )
        await storage.put(raster_key, write_geotiff(result), "image/tiff")
        await storage.put(preview_key, render_preview(result.ndvi), "image/png")
        metadata = {
            "analysis_id": str(analysis.id),
            "scene_id": selection.scene_id,
            "scene_date": selection.scene_date.isoformat(),
            "cloud_coverage": selection.cloud_coverage,
            "crs": result.crs,
            "transform": list(result.transform)[:6],
            "processing_units": client.processing_units_spent,
        }
        await storage.put(metadata_key, json.dumps(metadata).encode(), "application/json")
        return raster_key, preview_key

    def _persist_results(self, analysis: Analysis, per_lote: list[_LoteResult]) -> None:
        stats_rows = [
            AnalysisLoteStats(
                analysis_id=analysis.id,
                lote_id=item.lote.id,
                valid_pixels=item.stats.valid_pixels,
                total_pixels=item.stats.total_pixels,
                excluded_reason=item.stats.excluded_reason,
                mean=item.stats.mean,
                median=item.stats.median,
                std_dev=item.stats.std_dev,
                min_value=item.stats.min_value,
                max_value=item.stats.max_value,
                percentile_10=item.stats.percentile_10,
                percentile_90=item.stats.percentile_90,
            )
            for item in per_lote
        ]
        anomaly_rows = [
            Anomaly(
                analysis_id=analysis.id,
                lote_id=item.lote.id,
                geometry=from_shape(anomaly.geometry, srid=4326),
                centroid=from_shape(anomaly.centroid, srid=4326),
                area_m2=anomaly.area_m2,
                area_ratio_of_lote=anomaly.area_ratio_of_lote,
                severity=anomaly.severity,
                mean_zscore=anomaly.mean_zscore,
                mean_index_value=anomaly.mean_index_value,
                pixel_count=anomaly.pixel_count,
            )
            for item in per_lote
            for anomaly in item.anomalies
        ]
        self._analyses.add_results(stats_rows, anomaly_rows)

    def _complete(
        self,
        analysis: Analysis,
        result: NdviResult,
        selection: SceneSelection,
        per_lote: list[_LoteResult],
        raster_key: str,
        preview_key: str,
        client: SentinelGateway,
        clock: float,
    ) -> None:
        if analysis.force:
            self._supersede_previous(analysis, selection.scene_id)
        analysis.status = AnalysisStatus.COMPLETED
        analysis.completed_at = datetime.now(UTC)
        analysis.valid_pixels_ratio = result.valid_ratio
        analysis.processing_units_spent = client.processing_units_spent
        analysis.raster_key = raster_key
        analysis.preview_key = preview_key
        severities = Counter(
            anomaly.severity.value for item in per_lote for anomaly in item.anomalies
        )
        excluded = Counter(
            item.stats.excluded_reason.value
            for item in per_lote
            if item.stats.excluded_reason is not None
        )
        self._record(
            events.ANALYSIS_COMPLETED,
            analysis,
            {
                "scene_id": selection.scene_id,
                "scene_date": selection.scene_date.isoformat(),
                "cloud_coverage": round(selection.cloud_coverage, 4),
                "valid_pixels_ratio": round(result.valid_ratio, 4),
                "processing_units_spent": round(client.processing_units_spent, 4),
                # If values ever come back unscaled, these show it at once.
                "reflectance_percentiles": {
                    band: {"p1": p.p1, "p50": p.p50, "p99": p.p99}
                    for band, p in result.percentiles.items()
                },
                "lotes_measured": sum(
                    1 for item in per_lote if item.stats.mean is not None
                ),
                "lotes_excluded": dict(excluded),
                "anomalies_by_severity": dict(severities),
                "duration_s": round(time.monotonic() - clock, 2),
            },
        )
        if severities:
            self._record(
                events.ANOMALY_DETECTED,
                analysis,
                {"count": sum(severities.values()), "by_severity": dict(severities)},
            )
        self._session.commit()

    def _finish_without_scene(
        self,
        analysis: Analysis,
        detail: dict[str, Any],
        client: SentinelGateway,
        clock: float,
    ) -> None:
        analysis.status = AnalysisStatus.NO_SUITABLE_SCENE
        analysis.completed_at = datetime.now(UTC)
        analysis.error_detail = detail
        analysis.processing_units_spent = client.processing_units_spent
        self._record(
            events.ANALYSIS_NO_SUITABLE_SCENE,
            analysis,
            {
                "scenes_evaluated": detail.get("scenes_evaluated"),
                "best_cloud_fraction": detail.get("best_cloud_fraction"),
                "valid_pixels_ratio": detail.get("valid_pixels_ratio"),
                "processing_units_spent": round(client.processing_units_spent, 4),
                "duration_s": round(time.monotonic() - clock, 2),
            },
        )
        self._session.commit()

    def _fail(
        self,
        analysis_id: uuid.UUID,
        code: str,
        detail: dict[str, Any],
        client: SentinelGateway | None,
        clock: float | None,
    ) -> None:
        analysis = self._analyses.get(analysis_id)
        if analysis is None:
            return
        analysis.status = AnalysisStatus.FAILED
        analysis.error_code = code
        analysis.error_detail = detail
        analysis.completed_at = datetime.now(UTC)
        if client is not None:
            analysis.processing_units_spent = client.processing_units_spent
        details: dict[str, Any] = {"error_code": code}
        if clock is not None:
            details["duration_s"] = round(time.monotonic() - clock, 2)
        self._record(events.ANALYSIS_FAILED, analysis, details, AuditOutcome.FAILURE)
        self._session.commit()

    def fail_pending(
        self, analysis_id: uuid.UUID, code: str, detail: dict[str, Any]
    ) -> None:
        """Fail a PENDING analysis that cannot run at all (no credentials, ...)."""
        analysis = self._analyses.lock(analysis_id)
        if analysis is None or analysis.status is not AnalysisStatus.PENDING:
            self._session.rollback()
            return
        self._fail(analysis_id, code, detail, None, None)

    # --- zombies --------------------------------------------------------------------

    def expire_stale(self, now: datetime | None = None) -> int:
        """Fail analyses stuck in flight: RUNNING or PENDING past their timeout.

        Without this, a worker that dies mid-run leaves its analysis in
        RUNNING forever, and a job lost from the queue leaves it in PENDING;
        either way the predio can never be analysed again, because the
        one-in-flight index keeps refusing new requests.
        """
        moment = now or datetime.now(UTC)
        timeout = self._settings.analysis_job_timeout_s
        expired = self._analyses.stale_running(moment - timedelta(seconds=timeout))
        pending_timeout = self._settings.analysis_pending_timeout_s
        never_started = self._analyses.stale_pending(
            moment - timedelta(seconds=pending_timeout)
        )
        for analysis in never_started:
            analysis.status = AnalysisStatus.FAILED
            analysis.error_code = "QUEUE_TIMEOUT"
            analysis.error_detail = {
                "timeout_s": pending_timeout,
                "requested_at": analysis.requested_at.isoformat(),
            }
            analysis.completed_at = moment
            self._record(
                events.ANALYSIS_FAILED,
                analysis,
                {"error_code": "QUEUE_TIMEOUT", "timeout_s": pending_timeout},
                AuditOutcome.FAILURE,
            )
        for analysis in expired:
            started = analysis.started_at
            analysis.status = AnalysisStatus.FAILED
            analysis.error_code = "TIMEOUT"
            analysis.error_detail = {
                "timeout_s": timeout,
                "started_at": started.isoformat() if started else None,
            }
            analysis.completed_at = moment
            self._record(
                events.ANALYSIS_FAILED,
                analysis,
                {"error_code": "TIMEOUT", "timeout_s": timeout},
                AuditOutcome.FAILURE,
            )
        self._session.commit()
        return len(expired) + len(never_started)

    # --- reads and reviews (the API) ------------------------------------------------

    def get(self, predio_id: uuid.UUID, analysis_id: uuid.UUID) -> Analysis:
        """Return an analysis of the predio, or raise AnalysisNotFoundError."""
        analysis = self._analyses.get_in_predio(predio_id, analysis_id)
        if analysis is None:
            raise AnalysisNotFoundError
        return analysis

    def list_analyses(
        self, predio_id: uuid.UUID, filters: AnalysisFilters, params: PageParams
    ) -> Page[Analysis]:
        """Return a page of the predio's analyses."""
        return self._analyses.list_for_predio(predio_id, filters, params)

    def detail(self, predio_id: uuid.UUID, analysis_id: uuid.UUID) -> AnalysisDetail:
        """Return an analysis with its per-lote rows and anomaly counts."""
        analysis = self.get(predio_id, analysis_id)
        return AnalysisDetail(
            analysis,
            self._analyses.lote_stats(analysis.id),
            self._analyses.anomaly_counts(analysis.id),
        )

    def anomalies(
        self,
        predio_id: uuid.UUID,
        analysis_id: uuid.UUID,
        severity: AnomalySeverity | None = None,
    ) -> list[Anomaly]:
        """Return the anomalies of an analysis of the predio."""
        analysis = self.get(predio_id, analysis_id)
        return self._analyses.anomalies(analysis.id, severity)

    def review_anomaly(
        self,
        predio_id: uuid.UUID,
        anomaly_id: uuid.UUID,
        status: AnomalyReviewStatus,
        notes: str | None,
        actor: Actor,
    ) -> Anomaly:
        """Record an agronomist's verdict on an anomaly.

        A later review replaces the previous one on the row; the audit trail
        keeps every verdict, including what it replaced.
        """
        anomaly = self._analyses.get_anomaly_in_predio(predio_id, anomaly_id, lock=True)
        if anomaly is None:
            raise AnomalyNotFoundError
        analysis = self._analyses.get(anomaly.analysis_id)
        if analysis is None:  # the FK makes this unreachable; fail closed anyway
            raise AnomalyNotFoundError
        previous = anomaly.review_status
        anomaly.review_status = status
        anomaly.review_notes = notes
        anomaly.reviewed_by_user_id = actor.user_id
        anomaly.reviewed_at = datetime.now(UTC)
        self._record(
            events.ANOMALY_REVIEWED,
            analysis,
            {
                "anomaly_id": str(anomaly.id),
                "review_status": status.value,
                "previous_review_status": previous.value if previous else None,
                "severity": anomaly.severity.value,
                "has_notes": bool(notes),
            },
            actor=actor,
        )
        self._session.commit()
        return anomaly

    def record_raster_download(
        self, analysis: Analysis, artifact: str, actor: Actor
    ) -> None:
        """Note who downloaded which artifact of an analysis."""
        self._record(
            events.RASTER_DOWNLOADED, analysis, {"artifact": artifact}, actor=actor
        )
        self._session.commit()

    def lote_history(
        self,
        predio_id: uuid.UUID,
        lote_id: uuid.UUID,
        analysis_type: AnalysisType,
        date_from: date | None,
        date_to: date | None,
    ) -> list[LoteHistoryPoint]:
        """Return a lote's index over time, from vigente analyses only."""
        if LoteRepository(self._session).get_in_predio(predio_id, lote_id) is None:
            raise LoteNotFoundError
        return self._analyses.lote_history(lote_id, analysis_type, date_from, date_to)
