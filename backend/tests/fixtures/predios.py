"""Create predios directly in the database for tests that need one to exist.

Since phase 2, ``user_predio_roles.predio_id`` is a real foreign key, so any
test that grants a predio-scoped role needs an actual predio behind it.
"""

from __future__ import annotations

import uuid
from typing import Any

from geoalchemy2.shape import from_shape
from shapely.geometry import Point
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.modules.predios.models import Lote, Predio
from app.shared.enums import LoteType
from app.shared.geo import geojson_to_wkb, validate_geojson_geometry
from tests.fixtures import geometries as g


def derive_area_and_centroid(
    session: Session, geojson: dict[str, Any]
) -> tuple[float, Point]:
    """Compute area (m2, on the ellipsoid) and centroid in PostGIS.

    Coordinates travel as a bound parameter, never interpolated into SQL.
    """
    geometry = validate_geojson_geometry(geojson)
    row = session.execute(
        text(
            "SELECT ST_Area(geom::geography), ST_X(c), ST_Y(c) "
            "FROM (SELECT ST_GeomFromText(:wkt, 4326) AS geom) AS g, "
            "LATERAL ST_Centroid(g.geom) AS c"
        ),
        {"wkt": geometry.wkt},
    ).one()
    return float(row[0]), Point(float(row[1]), float(row[2]))


def create_predio(
    session: Session,
    owner_id: uuid.UUID,
    *,
    predio_id: uuid.UUID | None = None,
    geojson: dict[str, Any] | None = None,
    slug: str | None = None,
    name: str = "Viña Santa Elena",
) -> Predio:
    """Insert a predio owned by ``owner_id`` and return it."""
    shape_data = geojson or g.SQUARE
    area, centroid = derive_area_and_centroid(session, shape_data)
    predio = Predio(
        owner_user_id=owner_id,
        created_by_user_id=owner_id,
        name=name,
        slug=slug or f"predio-{uuid.uuid4().hex[:8]}",
        geometry=geojson_to_wkb(validate_geojson_geometry(shape_data)),
        centroid=from_shape(centroid, srid=4326),
        area_m2=area,
    )
    if predio_id is not None:
        predio.id = predio_id
    session.add(predio)
    session.flush()
    return predio


def create_lote(
    session: Session,
    predio: Predio,
    *,
    geojson: dict[str, Any] | None = None,
    name: str | None = None,
    lote_type: LoteType = LoteType.CUARTEL,
) -> Lote:
    """Insert a lote of ``predio`` and return it (the predio's SW quarter by default)."""
    shape_data = geojson or g.square(size=g.STEP / 2)
    area, centroid = derive_area_and_centroid(session, shape_data)
    lote = Lote(
        predio_id=predio.id,
        created_by_user_id=predio.owner_user_id,
        name=name or f"Cuartel {uuid.uuid4().hex[:6]}",
        lote_type=lote_type,
        geometry=geojson_to_wkb(validate_geojson_geometry(shape_data)),
        centroid=from_shape(centroid, srid=4326),
        area_m2=area,
    )
    session.add(lote)
    session.flush()
    return lote
