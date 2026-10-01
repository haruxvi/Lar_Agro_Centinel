"""Public DTOs of the analysis context.

Listings and details carry no geometry: anomaly shapes travel only in the
FeatureCollection of their own endpoint, and rasters only through storage.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.shared.enums import (
    AnalysisStatus,
    AnalysisType,
    AnomalyReviewStatus,
    AnomalySeverity,
    LoteStatsExclusion,
)

REVIEW_NOTES_MAX_LENGTH = 2000


class AnalysisCreate(BaseModel):
    """A request to analyse a predio. Missing dates default to the last month."""

    model_config = ConfigDict(extra="forbid")

    analysis_type: AnalysisType = AnalysisType.NDVI
    date_from: date | None = None
    date_to: date | None = None
    force: bool = False


class SupersedeCandidate(BaseModel):
    """The vigente analysis a forced recompute would most likely replace."""

    analysis_id: uuid.UUID
    reviewed_anomalies: int


class AnalysisAccepted(BaseModel):
    """202 answer: the analysis is queued; poll ``poll_url`` for its outcome."""

    analysis_id: uuid.UUID
    status: AnalysisStatus
    poll_url: str
    warning: str | None = None
    supersede_candidate: SupersedeCandidate | None = None


class AnalysisListItem(BaseModel):
    """One analysis in a listing."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    analysis_type: AnalysisType
    status: AnalysisStatus
    requested_date_from: date
    requested_date_to: date
    scene_date: date | None
    cloud_coverage: float | None
    valid_pixels_ratio: float | None
    processing_units_spent: float | None
    error_code: str | None
    force: bool
    resolved_to_analysis_id: uuid.UUID | None
    superseded_by_analysis_id: uuid.UUID | None
    requested_at: datetime
    completed_at: datetime | None


class AnalysisPage(BaseModel):
    """A page of analyses."""

    items: list[AnalysisListItem]
    total: int
    page: int
    page_size: int
    pages: int


class LoteStatsRead(BaseModel):
    """An index over one lote, or why it was not measured."""

    model_config = ConfigDict(from_attributes=True)

    lote_id: uuid.UUID
    excluded_reason: LoteStatsExclusion | None
    mean: float | None
    median: float | None
    std_dev: float | None
    min_value: float | None
    max_value: float | None
    percentile_10: float | None
    percentile_90: float | None
    valid_pixels: int
    total_pixels: int


class AnalysisRead(AnalysisListItem):
    """One analysis with everything a detail view shows."""

    scene_id: str | None
    started_at: datetime | None
    error_detail: dict[str, Any] | None
    raster_url: str | None
    preview_url: str | None
    anomalies_url: str | None
    anomaly_counts: dict[AnomalySeverity, int]
    lote_stats: list[LoteStatsRead]


class AnomalyReviewUpdate(BaseModel):
    """An agronomist's verdict on an anomaly."""

    model_config = ConfigDict(extra="forbid")

    status: AnomalyReviewStatus
    notes: str | None = Field(default=None, max_length=REVIEW_NOTES_MAX_LENGTH)


class AnomalyRead(BaseModel):
    """An anomaly's attributes, without its shape."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    analysis_id: uuid.UUID
    lote_id: uuid.UUID | None
    severity: AnomalySeverity
    area_m2: float
    area_ratio_of_lote: float
    mean_zscore: float
    mean_index_value: float
    pixel_count: int
    review_status: AnomalyReviewStatus | None
    review_notes: str | None
    reviewed_by_user_id: uuid.UUID | None
    reviewed_at: datetime | None


class AnomalyFeature(BaseModel):
    """One anomaly as a GeoJSON Feature."""

    type: Literal["Feature"] = "Feature"
    id: uuid.UUID
    geometry: dict[str, Any]
    properties: AnomalyRead


class AnomalyFeatureCollection(BaseModel):
    """The anomalies of one analysis, as GeoJSON."""

    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[AnomalyFeature]


class LoteHistoryPointRead(BaseModel):
    """A lote's statistics on one date."""

    model_config = ConfigDict(from_attributes=True)

    analysis_id: uuid.UUID
    scene_date: date
    mean: float
    median: float
    std_dev: float
    percentile_10: float
    percentile_90: float
    valid_pixels: int


class LoteHistoryRead(BaseModel):
    """A lote's index over time, from vigente analyses only."""

    lote_id: uuid.UUID
    analysis_type: AnalysisType
    points: list[LoteHistoryPointRead]
    insufficient_history: bool
    message: str | None = None


class PuUsageRead(BaseModel):
    """This month's Processing Units against the budget."""

    month: str
    spent: float
    budget: int
    remaining: float


class AnalysisStatusRead(BaseModel):
    """Operational state of satellite analysis (not the public health check)."""

    available: bool
    processing_units: PuUsageRead | None
