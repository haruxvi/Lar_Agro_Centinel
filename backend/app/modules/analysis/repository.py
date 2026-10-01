"""Data access for the analysis context."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import ColumnElement, Select, and_, func, select
from sqlalchemy.orm import Session

from app.modules.analysis.models import Analysis, AnalysisLoteStats, Anomaly
from app.shared.enums import AnalysisStatus, AnalysisType, AnomalySeverity
from app.shared.pagination import Page, PageParams, paginate

ACTIVE_STATUSES = (AnalysisStatus.PENDING, AnalysisStatus.RUNNING)


@dataclass(frozen=True)
class AnalysisFilters:
    """Optional narrowing of an analysis listing."""

    status: AnalysisStatus | None = None
    analysis_type: AnalysisType | None = None
    scene_date_from: date | None = None
    scene_date_to: date | None = None


@dataclass(frozen=True)
class LoteHistoryPoint:
    """One lote's statistics on one current analysis."""

    analysis_id: uuid.UUID
    scene_date: date
    mean: float
    median: float
    std_dev: float
    percentile_10: float
    percentile_90: float
    valid_pixels: int


def _current_completed() -> ColumnElement[bool]:
    """Match the vigente result: completed and not superseded."""
    return and_(
        Analysis.status == AnalysisStatus.COMPLETED,
        Analysis.superseded_by_analysis_id.is_(None),
    )


class AnalysisRepository:
    """Reads and writes analyses and their results."""

    def __init__(self, session: Session) -> None:
        """Bind the repository to a database session."""
        self._session = session

    def add(self, analysis: Analysis) -> Analysis:
        """Insert an analysis and flush it."""
        self._session.add(analysis)
        self._session.flush()
        return analysis

    def get(self, analysis_id: uuid.UUID) -> Analysis | None:
        """Return an analysis by id."""
        return self._session.get(Analysis, analysis_id)

    def get_in_predio(
        self, predio_id: uuid.UUID, analysis_id: uuid.UUID
    ) -> Analysis | None:
        """Return an analysis only if it belongs to ``predio_id``.

        Scoping by predio here is what stops an id from another predio being
        read through a predio the caller does have access to.
        """
        statement = select(Analysis).where(
            Analysis.id == analysis_id, Analysis.predio_id == predio_id
        )
        return self._session.execute(statement).scalar_one_or_none()

    def lock(self, analysis_id: uuid.UUID) -> Analysis | None:
        """Return an analysis with its row locked until the transaction ends.

        Two deliveries of the same job cannot both move it out of PENDING.
        """
        statement = select(Analysis).where(Analysis.id == analysis_id).with_for_update()
        return self._session.execute(statement).scalar_one_or_none()

    def find_active(
        self, predio_id: uuid.UUID, analysis_type: AnalysisType
    ) -> Analysis | None:
        """Return the analysis of this type pending or running, if any."""
        statement = select(Analysis).where(
            Analysis.predio_id == predio_id,
            Analysis.analysis_type == analysis_type,
            Analysis.status.in_(ACTIVE_STATUSES),
        )
        return self._session.execute(statement).scalar_one_or_none()

    def find_current_completed(
        self,
        predio_id: uuid.UUID,
        analysis_type: AnalysisType,
        scene_id: str,
        *,
        lock: bool = False,
    ) -> Analysis | None:
        """Return the vigente analysis of this scene, if there is one."""
        statement = select(Analysis).where(
            Analysis.predio_id == predio_id,
            Analysis.analysis_type == analysis_type,
            Analysis.scene_id == scene_id,
            _current_completed(),
        )
        if lock:
            statement = statement.with_for_update()
        return self._session.execute(statement).scalar_one_or_none()

    def latest_current_in_range(
        self,
        predio_id: uuid.UUID,
        analysis_type: AnalysisType,
        date_from: date,
        date_to: date,
    ) -> Analysis | None:
        """Return the newest vigente analysis whose scene falls in the range.

        Scene selection picks the most recent clear acquisition, so this is
        the analysis a forced recompute over the range would most likely
        supersede.
        """
        statement = (
            select(Analysis)
            .where(
                Analysis.predio_id == predio_id,
                Analysis.analysis_type == analysis_type,
                Analysis.scene_date >= date_from,
                Analysis.scene_date <= date_to,
                _current_completed(),
            )
            .order_by(Analysis.scene_date.desc(), Analysis.completed_at.desc())
            .limit(1)
        )
        return self._session.execute(statement).scalar_one_or_none()

    def count_reviewed_anomalies(self, analysis_id: uuid.UUID) -> int:
        """Return how many anomalies of the analysis an agronomist reviewed."""
        statement = select(func.count()).where(
            Anomaly.analysis_id == analysis_id, Anomaly.review_status.is_not(None)
        )
        return int(self._session.execute(statement).scalar_one())

    def stale_running(self, started_before: datetime) -> list[Analysis]:
        """Return RUNNING analyses started before ``started_before``, locked."""
        statement = (
            select(Analysis)
            .where(
                Analysis.status == AnalysisStatus.RUNNING,
                Analysis.started_at < started_before,
            )
            .with_for_update(skip_locked=True)
        )
        return list(self._session.execute(statement).scalars())

    def stale_pending(self, requested_before: datetime) -> list[Analysis]:
        """Return PENDING analyses requested before ``requested_before``, locked."""
        statement = (
            select(Analysis)
            .where(
                Analysis.status == AnalysisStatus.PENDING,
                Analysis.requested_at < requested_before,
            )
            .with_for_update(skip_locked=True)
        )
        return list(self._session.execute(statement).scalars())

    def add_results(
        self, stats: Iterable[AnalysisLoteStats], anomalies: Iterable[Anomaly]
    ) -> None:
        """Insert per-lote statistics and anomalies of an analysis."""
        self._session.add_all([*stats, *anomalies])
        self._session.flush()

    # --- reads for the API ----------------------------------------------------------

    def list_for_predio(
        self, predio_id: uuid.UUID, filters: AnalysisFilters, params: PageParams
    ) -> Page[Analysis]:
        """Return a page of the predio's analyses, newest request first."""
        statement: Select[tuple[Analysis]] = select(Analysis).where(
            Analysis.predio_id == predio_id
        )
        if filters.status is not None:
            statement = statement.where(Analysis.status == filters.status)
        if filters.analysis_type is not None:
            statement = statement.where(Analysis.analysis_type == filters.analysis_type)
        if filters.scene_date_from is not None:
            statement = statement.where(Analysis.scene_date >= filters.scene_date_from)
        if filters.scene_date_to is not None:
            statement = statement.where(Analysis.scene_date <= filters.scene_date_to)
        statement = statement.order_by(Analysis.requested_at.desc(), Analysis.id)
        return paginate(self._session, statement, params)

    def lote_stats(self, analysis_id: uuid.UUID) -> list[AnalysisLoteStats]:
        """Return every lote row of an analysis, excluded ones included."""
        statement = (
            select(AnalysisLoteStats)
            .where(AnalysisLoteStats.analysis_id == analysis_id)
            .order_by(AnalysisLoteStats.lote_id)
        )
        return list(self._session.execute(statement).scalars())

    def anomaly_counts(self, analysis_id: uuid.UUID) -> dict[AnomalySeverity, int]:
        """Return the number of anomalies of an analysis per severity."""
        statement = (
            select(Anomaly.severity, func.count())
            .where(Anomaly.analysis_id == analysis_id)
            .group_by(Anomaly.severity)
        )
        return {
            severity: int(count) for severity, count in self._session.execute(statement)
        }

    def anomalies(
        self, analysis_id: uuid.UUID, severity: AnomalySeverity | None = None
    ) -> list[Anomaly]:
        """Return an analysis's anomalies, most severe and largest first."""
        statement = select(Anomaly).where(Anomaly.analysis_id == analysis_id)
        if severity is not None:
            statement = statement.where(Anomaly.severity == severity)
        statement = statement.order_by(Anomaly.mean_zscore, Anomaly.area_m2.desc())
        return list(self._session.execute(statement).scalars())

    def get_anomaly_in_predio(
        self, predio_id: uuid.UUID, anomaly_id: uuid.UUID, *, lock: bool = False
    ) -> Anomaly | None:
        """Return an anomaly only if its analysis belongs to ``predio_id``."""
        statement = (
            select(Anomaly)
            .join(Analysis, Analysis.id == Anomaly.analysis_id)
            .where(Anomaly.id == anomaly_id, Analysis.predio_id == predio_id)
        )
        if lock:
            statement = statement.with_for_update(of=Anomaly)
        return self._session.execute(statement).scalar_one_or_none()

    def lote_history(
        self,
        lote_id: uuid.UUID,
        analysis_type: AnalysisType,
        date_from: date | None,
        date_to: date | None,
    ) -> list[LoteHistoryPoint]:
        """Return a lote's measured statistics over vigente analyses, by date.

        Superseded analyses are left out: a recomputed scene counts once.
        """
        statement = (
            select(AnalysisLoteStats, Analysis.scene_date)
            .join(Analysis, Analysis.id == AnalysisLoteStats.analysis_id)
            .where(
                AnalysisLoteStats.lote_id == lote_id,
                AnalysisLoteStats.excluded_reason.is_(None),
                Analysis.analysis_type == analysis_type,
                _current_completed(),
            )
            .order_by(Analysis.scene_date, Analysis.completed_at)
        )
        if date_from is not None:
            statement = statement.where(Analysis.scene_date >= date_from)
        if date_to is not None:
            statement = statement.where(Analysis.scene_date <= date_to)
        points: list[LoteHistoryPoint] = []
        for stats, scene_date in self._session.execute(statement):
            # Measured rows have every statistic (ck_..._measured_or_excluded).
            if (
                stats.mean is None
                or stats.median is None
                or stats.std_dev is None
                or stats.percentile_10 is None
                or stats.percentile_90 is None
            ):
                continue
            points.append(
                LoteHistoryPoint(
                    analysis_id=stats.analysis_id,
                    scene_date=scene_date,
                    mean=stats.mean,
                    median=stats.median,
                    std_dev=stats.std_dev,
                    percentile_10=stats.percentile_10,
                    percentile_90=stats.percentile_90,
                    valid_pixels=stats.valid_pixels,
                )
            )
        return points
