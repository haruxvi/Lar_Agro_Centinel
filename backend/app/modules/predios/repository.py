"""Data access and spatial queries for the predios context.

Every spatial computation runs in PostGIS and every coordinate reaches the
database as a bound parameter; nothing here builds SQL or WKT by string
interpolation. Metric results (areas, distances, buffers) cast to
``geography`` so they are measured on the ellipsoid, not on a flat projection.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from geoalchemy2 import Geography
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry
from sqlalchemy import ColumnElement, Float, cast, func, select
from sqlalchemy.orm import Session

from app.modules.predios.models import Lote, Predio
from app.shared.config import get_settings
from app.shared.pagination import Page, PageParams, paginate


def _geometry_param(geometry: BaseGeometry) -> ColumnElement[object]:
    """Bind a Shapely geometry as a PostGIS geometry parameter."""
    return func.ST_GeomFromText(geometry.wkt, get_settings().geo_srid)


def _point_param(lon: float, lat: float) -> ColumnElement[object]:
    """Bind a longitude/latitude pair as a PostGIS point parameter."""
    return func.ST_SetSRID(func.ST_MakePoint(lon, lat), get_settings().geo_srid)


def _metric_area(expression: ColumnElement[object]) -> ColumnElement[float]:
    """Area in square metres, measured on the ellipsoid."""
    return cast(func.ST_Area(cast(expression, Geography)), Float)


class PredioRepository:
    """Reads and writes ``predios`` rows and answers spatial questions."""

    def __init__(self, session: Session) -> None:
        """Bind the repository to a database session."""
        self._session = session

    # --- persistence ----------------------------------------------------------

    def create(self, predio: Predio) -> Predio:
        """Insert a predio and flush it."""
        self._session.add(predio)
        self._session.flush()
        return predio

    def get_by_id(
        self, predio_id: uuid.UUID, *, include_deleted: bool = False
    ) -> Predio | None:
        """Return a predio by id; soft-deleted ones only when asked for."""
        statement = select(Predio).where(Predio.id == predio_id)
        if not include_deleted:
            statement = statement.where(Predio.deleted_at.is_(None))
        return self._session.execute(statement).scalar_one_or_none()

    def lock(self, predio_id: uuid.UUID) -> Predio | None:
        """Return a live predio with its row locked until the transaction ends.

        Serialises changes that must see a consistent picture of the predio,
        such as removing a PROPIETARIO while another removal is in flight.
        """
        statement = (
            select(Predio)
            .where(Predio.id == predio_id, Predio.deleted_at.is_(None))
            .with_for_update()
        )
        return self._session.execute(statement).scalar_one_or_none()

    def get_by_slug(self, owner_user_id: uuid.UUID, slug: str) -> Predio | None:
        """Return an owner's live predio by slug."""
        statement = select(Predio).where(
            Predio.owner_user_id == owner_user_id,
            Predio.slug == slug,
            Predio.deleted_at.is_(None),
        )
        return self._session.execute(statement).scalar_one_or_none()

    def slug_exists(self, owner_user_id: uuid.UUID, slug: str) -> bool:
        """Return whether an owner already uses ``slug`` on a live predio."""
        return self.get_by_slug(owner_user_id, slug) is not None

    def list_by_owner(self, owner_user_id: uuid.UUID, params: PageParams) -> Page[Predio]:
        """Return one page of an owner's live predios, ordered by name."""
        statement = (
            select(Predio)
            .where(Predio.owner_user_id == owner_user_id, Predio.deleted_at.is_(None))
            .order_by(Predio.name, Predio.id)
        )
        return paginate(self._session, statement, params)

    def list_by_ids(
        self, predio_ids: Sequence[uuid.UUID], params: PageParams
    ) -> Page[Predio]:
        """Return one page of the given live predios, ordered by name."""
        if not predio_ids:
            return Page(items=[], total=0, page=params.page, page_size=params.page_size)
        statement = (
            select(Predio)
            .where(Predio.id.in_(predio_ids), Predio.deleted_at.is_(None))
            .order_by(Predio.name, Predio.id)
        )
        return paginate(self._session, statement, params)

    def list_all(self, params: PageParams) -> Page[Predio]:
        """Return one page of every live predio, for global read-only roles."""
        statement = (
            select(Predio)
            .where(Predio.deleted_at.is_(None))
            .order_by(Predio.name, Predio.id)
        )
        return paginate(self._session, statement, params)

    def update(self, predio: Predio, changes: Mapping[str, object]) -> Predio:
        """Apply column changes to a predio and flush them."""
        for field, value in changes.items():
            setattr(predio, field, value)
        self._session.flush()
        return predio

    def soft_delete(self, predio: Predio, deleted_by: uuid.UUID) -> None:
        """Mark a predio deleted. Rows are never physically removed."""
        predio.deleted_at = datetime.now(UTC)
        predio.deleted_by_user_id = deleted_by
        predio.is_active = False
        self._session.flush()

    # --- spatial computations -------------------------------------------------

    def compute_area_m2(self, geometry: BaseGeometry) -> float:
        """Return the geometry's area in square metres (ellipsoidal)."""
        area = self._session.execute(select(_metric_area(_geometry_param(geometry))))
        return float(area.scalar_one())

    def compute_centroid(self, geometry: BaseGeometry) -> Point:
        """Return the geometry's centroid as a Shapely point."""
        centroid = func.ST_Centroid(_geometry_param(geometry))
        row = self._session.execute(
            select(func.ST_X(centroid), func.ST_Y(centroid))
        ).one()
        return Point(float(row[0]), float(row[1]))

    def find_predios_containing_point(self, lon: float, lat: float) -> list[Predio]:
        """Return live predios whose geometry contains the point (GiST-indexed)."""
        statement = select(Predio).where(
            func.ST_Contains(Predio.geometry, _point_param(lon, lat)),
            Predio.deleted_at.is_(None),
        )
        return list(self._session.execute(statement).scalars())

    def find_predios_within_radius(
        self, lon: float, lat: float, radius_m: float
    ) -> list[Predio]:
        """Return live predios whose centroid lies within ``radius_m`` metres."""
        statement = select(Predio).where(
            func.ST_DWithin(
                cast(Predio.centroid, Geography),
                cast(_point_param(lon, lat), Geography),
                radius_m,
            ),
            Predio.deleted_at.is_(None),
        )
        return list(self._session.execute(statement).scalars())

    def find_overlapping_predios(
        self,
        geometry: BaseGeometry,
        *,
        exclude_predio_id: uuid.UUID | None = None,
        owner_user_id: uuid.UUID | None = None,
    ) -> list[tuple[Predio, float]]:
        """Return live predios sharing area with ``geometry``, with the overlap in m2.

        Predios that only touch along a border share no area and are ignored.
        ``owner_user_id`` narrows the search to one owner's predios, which is
        what a caller may be told about.
        """
        candidate = _geometry_param(geometry)
        overlap = _metric_area(func.ST_Intersection(Predio.geometry, candidate))
        statement = select(Predio, overlap).where(
            # ST_Intersects uses the GiST index; the area filter drops touches.
            func.ST_Intersects(Predio.geometry, candidate),
            overlap > 0,
            Predio.deleted_at.is_(None),
        )
        if exclude_predio_id is not None:
            statement = statement.where(Predio.id != exclude_predio_id)
        if owner_user_id is not None:
            statement = statement.where(Predio.owner_user_id == owner_user_id)
        return [(row[0], float(row[1])) for row in self._session.execute(statement)]

    def lote_within_predio(
        self,
        lote_geometry: BaseGeometry,
        predio_geometry: BaseGeometry,
        tolerance_m: float,
    ) -> bool:
        """Return whether a lote lies inside its predio, give or take ``tolerance_m``.

        The tolerance absorbs the imprecision of hand digitising a boundary.
        """
        buffered = cast(
            func.ST_Buffer(
                cast(_geometry_param(predio_geometry), Geography), tolerance_m
            ),
            Predio.geometry.type,
        )
        within = self._session.execute(
            select(func.ST_Within(_geometry_param(lote_geometry), buffered))
        )
        return bool(within.scalar_one())

    def area_outside_m2(
        self, inner_geometry: BaseGeometry, outer_geometry: BaseGeometry
    ) -> float:
        """Return how many m2 of ``inner_geometry`` fall outside ``outer_geometry``."""
        difference = func.ST_Difference(
            _geometry_param(inner_geometry), _geometry_param(outer_geometry)
        )
        return float(self._session.execute(select(_metric_area(difference))).scalar_one())


class LoteRepository:
    """Reads and writes ``lotes`` rows, always within a predio."""

    def __init__(self, session: Session) -> None:
        """Bind the repository to a database session."""
        self._session = session

    def create(self, lote: Lote) -> Lote:
        """Insert a lote and flush it."""
        self._session.add(lote)
        self._session.flush()
        return lote

    def get_in_predio(self, predio_id: uuid.UUID, lote_id: uuid.UUID) -> Lote | None:
        """Return a live lote only if it belongs to ``predio_id``.

        Scoping by predio in the query means a lote id taken from another
        predio is simply not found, rather than found and then refused.
        """
        statement = select(Lote).where(
            Lote.id == lote_id,
            Lote.predio_id == predio_id,
            Lote.deleted_at.is_(None),
        )
        return self._session.execute(statement).scalar_one_or_none()

    def list_for_predio(self, predio_id: uuid.UUID) -> list[Lote]:
        """Return a predio's live lotes, ordered by name."""
        statement = (
            select(Lote)
            .where(Lote.predio_id == predio_id, Lote.deleted_at.is_(None))
            .order_by(Lote.name, Lote.id)
        )
        return list(self._session.execute(statement).scalars())

    def count_active(self, predio_id: uuid.UUID) -> int:
        """Return how many live lotes a predio has."""
        statement = select(func.count()).where(
            Lote.predio_id == predio_id, Lote.deleted_at.is_(None)
        )
        return int(self._session.execute(statement).scalar_one())

    def find_overlapping_lotes(
        self,
        predio_id: uuid.UUID,
        geometry: BaseGeometry,
        *,
        exclude_lote_id: uuid.UUID | None = None,
    ) -> list[tuple[Lote, float]]:
        """Return sibling lotes sharing area with ``geometry``, with the overlap in m2."""
        candidate = _geometry_param(geometry)
        overlap = _metric_area(func.ST_Intersection(Lote.geometry, candidate))
        statement = select(Lote, overlap).where(
            Lote.predio_id == predio_id,
            func.ST_Intersects(Lote.geometry, candidate),
            overlap > 0,
            Lote.deleted_at.is_(None),
        )
        if exclude_lote_id is not None:
            statement = statement.where(Lote.id != exclude_lote_id)
        return [(row[0], float(row[1])) for row in self._session.execute(statement)]

    def sum_lotes_area(self, predio_id: uuid.UUID) -> float:
        """Return the summed area of a predio's live lotes, in m2."""
        statement = select(func.coalesce(func.sum(Lote.area_m2), 0.0)).where(
            Lote.predio_id == predio_id, Lote.deleted_at.is_(None)
        )
        return float(self._session.execute(statement).scalar_one())

    def update(self, lote: Lote, changes: Mapping[str, object]) -> Lote:
        """Apply column changes to a lote and flush them."""
        for field, value in changes.items():
            setattr(lote, field, value)
        self._session.flush()
        return lote

    def soft_delete(self, lote: Lote, deleted_by: uuid.UUID) -> None:
        """Mark a lote deleted. Rows are never physically removed."""
        lote.deleted_at = datetime.now(UTC)
        lote.deleted_by_user_id = deleted_by
        lote.is_active = False
        self._session.flush()
