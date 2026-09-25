"""Public DTOs of the predios context.

Listings never carry geometries: a list of predios or lotes returns centroids
and areas, and a single resource returns its full GeoJSON.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.shared.enums import LoteType
from app.shared.roles import Role

SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"

GeoJSONGeometry = dict[str, Any]


class _Input(BaseModel):
    """Request bodies reject unknown fields rather than silently drop them."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _reject_nulls(model: BaseModel, fields: tuple[str, ...]) -> None:
    for name in fields:
        if name in model.model_fields_set and getattr(model, name) is None:
            raise ValueError(f"{name} cannot be null")


# --- predios ------------------------------------------------------------------


class PredioCreate(_Input):
    """Body of ``POST /predios``."""

    name: str = Field(min_length=1, max_length=255)
    slug: str | None = Field(default=None, max_length=120, pattern=SLUG_PATTERN)
    description: str | None = Field(default=None, max_length=5000)
    region: str | None = Field(default=None, max_length=120)
    comuna: str | None = Field(default=None, max_length=120)
    address: str | None = Field(default=None, max_length=255)
    rol_sii: str | None = Field(default=None, max_length=32)
    geometry: GeoJSONGeometry
    accept_repair: bool = False
    """Store an automatic repair even if it needs confirmation (see the 422)."""


class PredioUpdate(_Input):
    """Body of ``PATCH /predios/{id}``. Only the fields sent are changed."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    slug: str | None = Field(default=None, max_length=120, pattern=SLUG_PATTERN)
    description: str | None = Field(default=None, max_length=5000)
    region: str | None = Field(default=None, max_length=120)
    comuna: str | None = Field(default=None, max_length=120)
    address: str | None = Field(default=None, max_length=255)
    rol_sii: str | None = Field(default=None, max_length=32)
    geometry: GeoJSONGeometry | None = None
    accept_repair: bool = False
    """Store an automatic repair even if it needs confirmation (see the 422)."""

    @model_validator(mode="after")
    def _required_fields_are_not_nulled(self) -> Self:
        _reject_nulls(self, ("name", "slug", "geometry"))
        return self


class GeometryWarningRead(BaseModel):
    """A warning raised by an accepted geometry."""

    code: str
    detail: dict[str, Any] = Field(default_factory=dict)


class PredioListItem(BaseModel):
    """A predio in a listing: no geometry, only where it is and how big."""

    id: uuid.UUID
    name: str
    slug: str
    area_m2: float
    area_ha: float
    region: str | None
    comuna: str | None
    centroid: tuple[float, float]
    """(longitude, latitude)."""
    created_at: datetime


class PredioRead(PredioListItem):
    """A single predio, with its boundary as GeoJSON."""

    owner_user_id: uuid.UUID
    description: str | None
    address: str | None
    rol_sii: str | None
    bbox: tuple[float, float, float, float]
    """(min_lon, min_lat, max_lon, max_lat)."""
    geometry: GeoJSONGeometry
    geometry_was_repaired: bool
    geometry_repair_area_delta_m2: float | None
    """Area change of the stored repair; null if unrepaired or unmeasurable."""
    updated_at: datetime


class PredioWriteResponse(BaseModel):
    """A predio that was just written, and the warnings its geometry raised."""

    predio: PredioRead
    warnings: list[GeometryWarningRead]


class PredioPage(BaseModel):
    """One page of predios."""

    items: list[PredioListItem]
    total: int
    page: int
    page_size: int
    pages: int


class PredioSummaryRead(BaseModel):
    """Headline figures of a predio."""

    predio_id: uuid.UUID
    area_m2: float
    area_ha: float
    lotes_count: int
    lotes_area_m2: float
    unassigned_area_m2: float
    coverage_ratio: float
    lotes_by_type: dict[LoteType, int]
    centroid: tuple[float, float]
    bbox: tuple[float, float, float, float]


class GeometryValidationRequest(_Input):
    """Body of ``POST /predios/validate-geometry``."""

    geometry: GeoJSONGeometry


class GeometryRepairRead(BaseModel):
    """What an automatic repair did to the area, and whether it needs confirming."""

    area_before_m2: float
    area_after_m2: float
    area_change_m2: float
    area_change_ratio: float | None
    """Null when the change cannot be measured: see ``reason``."""
    threshold_ratio: float
    ignore_below_m2: float
    reason: str
    """WITHIN_THRESHOLD, AREA_CHANGE_ABOVE_THRESHOLD, NOT_MEASURABLE_ZERO_AREA
    or NOT_MEASURABLE_SELF_INTERSECTION."""
    requires_confirmation: bool
    """Whether storing it will need ``accept_repair: true``."""


class GeometryValidationRead(BaseModel):
    """What a geometry would become if it were stored. Nothing is written.

    ``geometry`` is always the form that would be stored, repaired or not, so
    the client can preview it before trying to save.
    """

    valid: bool
    geometry: GeoJSONGeometry
    repaired: bool
    repair: GeometryRepairRead | None
    area_m2: float
    area_ha: float
    vertex_count: int
    centroid: tuple[float, float]
    bbox: tuple[float, float, float, float]
    warnings: list[GeometryWarningRead]


# --- lotes --------------------------------------------------------------------


class _LoteAttributes(_Input):
    code: str | None = Field(default=None, max_length=64)
    crop_type: str | None = Field(default=None, max_length=120)
    variety: str | None = Field(default=None, max_length=120)
    planting_year: int | None = Field(default=None, ge=1900, le=2100)
    plant_count: int | None = Field(default=None, ge=0)
    row_spacing_m: float | None = Field(default=None, gt=0, le=100)
    plant_spacing_m: float | None = Field(default=None, gt=0, le=100)
    notes: str | None = Field(default=None, max_length=5000)


class LoteCreate(_LoteAttributes):
    """Body of ``POST /predios/{id}/lotes``."""

    name: str = Field(min_length=1, max_length=255)
    lote_type: LoteType = LoteType.OTRO
    geometry: GeoJSONGeometry
    accept_repair: bool = False
    """Store an automatic repair even if it needs confirmation (see the 422)."""


class LoteUpdate(_LoteAttributes):
    """Body of ``PATCH /predios/{id}/lotes/{lote_id}``."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    lote_type: LoteType | None = None
    geometry: GeoJSONGeometry | None = None
    accept_repair: bool = False
    """Store an automatic repair even if it needs confirmation (see the 422)."""

    @model_validator(mode="after")
    def _required_fields_are_not_nulled(self) -> Self:
        _reject_nulls(self, ("name", "lote_type", "geometry"))
        return self


class LoteListItem(BaseModel):
    """A lote in a listing: no geometry."""

    id: uuid.UUID
    predio_id: uuid.UUID
    name: str
    code: str | None
    lote_type: LoteType
    area_m2: float
    area_ha: float
    crop_type: str | None
    variety: str | None
    centroid: tuple[float, float]


class LoteRead(LoteListItem):
    """A single lote, with its boundary as GeoJSON."""

    planting_year: int | None
    plant_count: int | None
    row_spacing_m: float | None
    plant_spacing_m: float | None
    notes: str | None
    geometry: GeoJSONGeometry
    geometry_was_repaired: bool
    geometry_repair_area_delta_m2: float | None
    """Area change of the stored repair; null if unrepaired or unmeasurable."""
    created_at: datetime
    updated_at: datetime


class LoteWriteResponse(BaseModel):
    """A lote that was just written, and the warnings its geometry raised."""

    lote: LoteRead
    warnings: list[GeometryWarningRead]


class LoteImportResponse(BaseModel):
    """The lotes created by an import."""

    created: list[LoteListItem]
    warnings: dict[int, list[GeometryWarningRead]]
    """Warnings by feature index; features without warnings are omitted."""


# --- membership ---------------------------------------------------------------


class PredioMemberCreate(_Input):
    """Body of ``POST /predios/{id}/users``."""

    email: EmailStr
    role: Role
    expires_at: datetime | None = None
    """Timezone-aware. Omitted means the grant does not expire."""


class PredioMemberRead(BaseModel):
    """A role held by a user on a predio."""

    user_id: uuid.UUID
    email: str
    full_name: str
    role: Role
    granted_at: datetime
    expires_at: datetime | None
    granted_by_user_id: uuid.UUID | None
