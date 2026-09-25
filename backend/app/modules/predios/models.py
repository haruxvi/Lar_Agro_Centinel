"""Persistence models for the predios context.

Geometries are stored in SRID 4326. Areas are derived in the database by
casting to ``geography``, never taken from the client: see
docs/decisions/ADR-003-geospatial-model.md.

PostGIS type modifiers cannot express "Polygon or MultiPolygon", so the
columns are generic geometries with the SRID pinned by the type modifier and
the allowed shapes pinned by a CHECK constraint.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from geoalchemy2 import Geometry, WKBElement
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db import Base
from app.shared.enums import LoteType

SRID = 4326

# Spatial indexes are declared explicitly below (and created by the migration),
# so GeoAlchemy2 must not add its own.
_AREA_GEOMETRY = Geometry(geometry_type="GEOMETRY", srid=SRID, spatial_index=False)
_POINT = Geometry(geometry_type="POINT", srid=SRID, spatial_index=False)

_POLYGONAL = "ST_GeometryType({column}) IN ('ST_Polygon', 'ST_MultiPolygon')"


class Predio(Base):
    """A farm or estate: the unit every other piece of data hangs from."""

    __tablename__ = "predios"
    __table_args__ = (
        CheckConstraint("area_m2 > 0", name="ck_predios_area_positive"),
        # A delta only makes sense for a repaired geometry; NULL for a repaired
        # one means the change could not be measured.
        CheckConstraint(
            "geometry_was_repaired OR geometry_repair_area_delta_m2 IS NULL",
            name="ck_predios_repair_delta_requires_repair",
        ),
        CheckConstraint(
            _POLYGONAL.format(column="geometry"), name="ck_predios_polygonal"
        ),
        Index("ix_predios_geometry", "geometry", postgresql_using="gist"),
        Index("ix_predios_centroid", "centroid", postgresql_using="gist"),
        Index(
            "uq_predios_owner_slug_active",
            "owner_user_id",
            "slug",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_predios_active",
            "deleted_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(120), index=True)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    geometry: Mapped[WKBElement] = mapped_column(_AREA_GEOMETRY)
    centroid: Mapped[WKBElement] = mapped_column(_POINT)
    area_m2: Mapped[float] = mapped_column(Float)
    # Traceability of automatic repairs: area feeds doses per hectare, so a
    # stored boundary that is not exactly what was drawn must say so.
    geometry_was_repaired: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    geometry_repair_area_delta_m2: Mapped[float | None] = mapped_column(
        Float, default=None
    )
    region: Mapped[str | None] = mapped_column(String(120), default=None)
    comuna: Mapped[str | None] = mapped_column(String(120), default=None)
    address: Mapped[str | None] = mapped_column(String(255), default=None)
    rol_sii: Mapped[str | None] = mapped_column(String(32), default=None)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    deleted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )


class Lote(Base):
    """A subdivision of a predio: a vineyard block, a paddock, a plot."""

    __tablename__ = "lotes"
    __table_args__ = (
        CheckConstraint("area_m2 > 0", name="ck_lotes_area_positive"),
        # A delta only makes sense for a repaired geometry; NULL for a repaired
        # one means the change could not be measured.
        CheckConstraint(
            "geometry_was_repaired OR geometry_repair_area_delta_m2 IS NULL",
            name="ck_lotes_repair_delta_requires_repair",
        ),
        CheckConstraint(_POLYGONAL.format(column="geometry"), name="ck_lotes_polygonal"),
        Index("ix_lotes_geometry", "geometry", postgresql_using="gist"),
        Index(
            "uq_lotes_predio_name_active",
            "predio_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    predio_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("predios.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    code: Mapped[str | None] = mapped_column(String(64), default=None)
    lote_type: Mapped[LoteType] = mapped_column(
        Enum(LoteType, native_enum=False, length=32)
    )
    geometry: Mapped[WKBElement] = mapped_column(_AREA_GEOMETRY)
    centroid: Mapped[WKBElement] = mapped_column(_POINT)
    area_m2: Mapped[float] = mapped_column(Float)
    # Traceability of automatic repairs: area feeds doses per hectare, so a
    # stored boundary that is not exactly what was drawn must say so.
    geometry_was_repaired: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    geometry_repair_area_delta_m2: Mapped[float | None] = mapped_column(
        Float, default=None
    )
    crop_type: Mapped[str | None] = mapped_column(String(120), default=None)
    variety: Mapped[str | None] = mapped_column(String(120), default=None)
    planting_year: Mapped[int | None] = mapped_column(Integer, default=None)
    plant_count: Mapped[int | None] = mapped_column(Integer, default=None)
    row_spacing_m: Mapped[float | None] = mapped_column(Float, default=None)
    plant_spacing_m: Mapped[float | None] = mapped_column(Float, default=None)
    notes: Mapped[str | None] = mapped_column(Text, default=None)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    deleted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
