"""Persistence models for the analysis context.

Invariants live in the database, not only in the service: a check constraint
cannot be skipped by a script, a worker or a future endpoint that forgets.
See docs/decisions/ADR-004-satellite-analysis.md.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from geoalchemy2 import Geometry, WKBElement
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db import Base
from app.shared.enums import (
    AnalysisStatus,
    AnalysisType,
    AnomalyReviewStatus,
    AnomalySeverity,
    LoteStatsExclusion,
)

SRID = 4326

_STATUS = Enum(AnalysisStatus, native_enum=False, length=32)

# A lote's statistics are all present or all absent: never a mean without its
# spread, never a number for a lote that was not measured.
_STAT_COLUMNS = (
    "mean",
    "median",
    "std_dev",
    "min_value",
    "max_value",
    "percentile_10",
    "percentile_90",
)
_ALL_STATS_PRESENT = " AND ".join(f"{column} IS NOT NULL" for column in _STAT_COLUMNS)
_ALL_STATS_ABSENT = " AND ".join(f"{column} IS NULL" for column in _STAT_COLUMNS)


class Analysis(Base):
    """One request to compute an index over a predio, and its outcome."""

    __tablename__ = "analyses"
    __table_args__ = (
        CheckConstraint(
            "requested_date_from <= requested_date_to",
            name="ck_analyses_date_range_ordered",
        ),
        CheckConstraint(
            "cloud_coverage IS NULL OR (cloud_coverage >= 0 AND cloud_coverage <= 1)",
            name="ck_analyses_cloud_coverage_ratio",
        ),
        CheckConstraint(
            "valid_pixels_ratio IS NULL "
            "OR (valid_pixels_ratio >= 0 AND valid_pixels_ratio <= 1)",
            name="ck_analyses_valid_pixels_ratio",
        ),
        CheckConstraint(
            "processing_units_spent IS NULL OR processing_units_spent >= 0",
            name="ck_analyses_pu_non_negative",
        ),
        # A completed analysis always knows its scene: the idempotency key
        # below would otherwise let duplicates through on NULL.
        CheckConstraint(
            "status <> 'COMPLETED' OR (scene_id IS NOT NULL "
            "AND scene_date IS NOT NULL AND completed_at IS NOT NULL)",
            name="ck_analyses_completed_has_scene",
        ),
        CheckConstraint(
            "status <> 'FAILED' OR error_code IS NOT NULL",
            name="ck_analyses_failed_has_error_code",
        ),
        # A duplicate always says where the result is, and only a duplicate
        # points elsewhere.
        CheckConstraint(
            "(status = 'DUPLICATE_SCENE') = (resolved_to_analysis_id IS NOT NULL)",
            name="ck_analyses_duplicate_resolved",
        ),
        # Only a completed analysis can be replaced by a forced recompute.
        CheckConstraint(
            "superseded_by_analysis_id IS NULL OR status = 'COMPLETED'",
            name="ck_analyses_superseded_only_completed",
        ),
        # Idempotency: one current result per (predio, type, scene). A forced
        # recompute supersedes the old one instead of deleting it.
        Index(
            "uq_analyses_completed_scene",
            "predio_id",
            "analysis_type",
            "scene_id",
            unique=True,
            postgresql_where=text(
                "status = 'COMPLETED' AND superseded_by_analysis_id IS NULL"
            ),
        ),
        # At most one analysis of a type in flight per predio, enforced here
        # so two concurrent requests cannot both enqueue.
        Index(
            "uq_analyses_one_active_per_type",
            "predio_id",
            "analysis_type",
            unique=True,
            postgresql_where=text("status IN ('PENDING', 'RUNNING')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    predio_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("predios.id", ondelete="RESTRICT"), index=True
    )
    analysis_type: Mapped[AnalysisType] = mapped_column(
        Enum(AnalysisType, native_enum=False, length=32)
    )
    status: Mapped[AnalysisStatus] = mapped_column(_STATUS, index=True)
    requested_date_from: Mapped[date] = mapped_column(Date)
    requested_date_to: Mapped[date] = mapped_column(Date)
    scene_date: Mapped[date | None] = mapped_column(Date, default=None, index=True)
    scene_id: Mapped[str | None] = mapped_column(String(512), default=None)
    cloud_coverage: Mapped[float | None] = mapped_column(Float, default=None)
    """Cloudy fraction over the predio, not over the tile."""
    valid_pixels_ratio: Mapped[float | None] = mapped_column(Float, default=None)
    processing_units_spent: Mapped[float | None] = mapped_column(Float, default=None)
    raster_key: Mapped[str | None] = mapped_column(String(512), default=None)
    preview_key: Mapped[str | None] = mapped_column(String(512), default=None)
    error_code: Mapped[str | None] = mapped_column(String(64), default=None)
    error_detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    """Scenes evaluated and their cloud cover, or a failure's diagnosis."""
    force: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    """Recompute even if the scene already has a current result."""
    superseded_by_analysis_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analyses.id", ondelete="RESTRICT"), default=None
    )
    """The forced recompute that replaced this result; kept, never deleted."""
    resolved_to_analysis_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analyses.id", ondelete="RESTRICT"), default=None
    )
    """For DUPLICATE_SCENE: the existing analysis that answers this request."""
    requested_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )


class AnalysisLoteStats(Base):
    """An index's statistics over one lote, or why there are none."""

    __tablename__ = "analysis_lote_stats"
    __table_args__ = (
        UniqueConstraint("analysis_id", "lote_id", name="uq_analysis_lote_stats_lote"),
        CheckConstraint(
            "valid_pixels >= 0 AND total_pixels >= 0 AND valid_pixels <= total_pixels",
            name="ck_analysis_lote_stats_pixel_counts",
        ),
        CheckConstraint(
            f"(excluded_reason IS NULL AND {_ALL_STATS_PRESENT}) "
            f"OR (excluded_reason IS NOT NULL AND {_ALL_STATS_ABSENT})",
            name="ck_analysis_lote_stats_measured_or_excluded",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    analysis_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), index=True
    )
    lote_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("lotes.id", ondelete="RESTRICT"), index=True
    )
    mean: Mapped[float | None] = mapped_column(Float, default=None)
    median: Mapped[float | None] = mapped_column(Float, default=None)
    std_dev: Mapped[float | None] = mapped_column(Float, default=None)
    min_value: Mapped[float | None] = mapped_column(Float, default=None)
    max_value: Mapped[float | None] = mapped_column(Float, default=None)
    percentile_10: Mapped[float | None] = mapped_column(Float, default=None)
    percentile_90: Mapped[float | None] = mapped_column(Float, default=None)
    valid_pixels: Mapped[int] = mapped_column(Integer)
    total_pixels: Mapped[int] = mapped_column(Integer)
    excluded_reason: Mapped[LoteStatsExclusion | None] = mapped_column(
        Enum(LoteStatsExclusion, native_enum=False, length=48), default=None
    )


class Anomaly(Base):
    """A zone significantly worse than the rest of its lote: a hypothesis.

    It becomes a finding only when an agronomist reviews it; those reviews
    are what will calibrate the severity cuts (see ADR-004).
    """

    __tablename__ = "anomalies"
    __table_args__ = (
        CheckConstraint("area_m2 > 0", name="ck_anomalies_area_positive"),
        CheckConstraint(
            "area_ratio_of_lote > 0 AND area_ratio_of_lote <= 1",
            name="ck_anomalies_area_ratio",
        ),
        CheckConstraint("pixel_count > 0", name="ck_anomalies_pixel_count_positive"),
        # Anomalies are, by definition, below their lote's mean.
        CheckConstraint("mean_zscore < 0", name="ck_anomalies_zscore_negative"),
        # A review is all three fields or none of them.
        CheckConstraint(
            "(review_status IS NULL) = (reviewed_at IS NULL) "
            "AND (review_status IS NULL) = (reviewed_by_user_id IS NULL)",
            name="ck_anomalies_review_complete",
        ),
        Index("ix_anomalies_geometry", "geometry", postgresql_using="gist"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    analysis_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("analyses.id", ondelete="CASCADE"), index=True
    )
    lote_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("lotes.id", ondelete="RESTRICT"), index=True, default=None
    )
    geometry: Mapped[WKBElement] = mapped_column(
        Geometry(geometry_type="POLYGON", srid=SRID, spatial_index=False)
    )
    centroid: Mapped[WKBElement] = mapped_column(
        Geometry(geometry_type="POINT", srid=SRID, spatial_index=False)
    )
    area_m2: Mapped[float] = mapped_column(Float)
    area_ratio_of_lote: Mapped[float] = mapped_column(Float)
    severity: Mapped[AnomalySeverity] = mapped_column(
        Enum(AnomalySeverity, native_enum=False, length=16)
    )
    mean_zscore: Mapped[float] = mapped_column(Float)
    mean_index_value: Mapped[float] = mapped_column(Float)
    pixel_count: Mapped[int] = mapped_column(Integer)
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), default=None
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    review_status: Mapped[AnomalyReviewStatus | None] = mapped_column(
        Enum(AnomalyReviewStatus, native_enum=False, length=32), default=None
    )
    review_notes: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
