"""Geospatial helpers shared by predios and, later, captures and analysis.

Storage is SRID 4326 (WGS84), the reference system of GeoJSON, drone GPS and
satellite products. Metric computations cast to ``geography`` in the database
rather than projecting here: see docs/decisions/ADR-003-geospatial-model.md.

Untrusted GeoJSON is checked structurally *before* it reaches Shapely. A
polygon with millions of vertices costs memory and CPU the moment it is parsed,
so the vertex budget and coordinate ranges are enforced on the raw structure.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, cast

import shapely
from geoalchemy2.elements import WKBElement
from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import MultiPolygon, Polygon, box, mapping, shape
from shapely.geometry.base import BaseGeometry

from app.shared.config import get_settings
from app.shared.exceptions import DomainError

ALLOWED_GEOMETRY_TYPES: Final = frozenset({"Polygon", "MultiPolygon"})

LON_RANGE: Final = (-180.0, 180.0)
LAT_RANGE: Final = (-90.0, 90.0)

# Approximate bounding box of continental and insular Chile: Rapa Nui to the
# west, the Desventuradas and Juan Fernández islands, down to Cape Horn.
# (min_lon, min_lat, max_lon, max_lat)
CHILE_BBOX: Final = (-109.6, -56.6, -66.0, -17.4)

# make_valid(method="structure") needs GEOS 3.10+. There is deliberately no
# fallback to the old "linework" method: a repair that behaves differently
# depending on the machine it runs on is worse than no repair at all.
REQUIRED_GEOS: Final = (3, 10, 0)


def ensure_geos_supports_structure(version: tuple[int, int, int] | None = None) -> None:
    """Refuse to start when the installed GEOS cannot repair with "structure"."""
    found = tuple(version or shapely.geos_version)
    if found < REQUIRED_GEOS:
        required = ".".join(map(str, REQUIRED_GEOS))
        raise RuntimeError(
            f"GEOS {'.'.join(map(str, found))} is too old: geometry repair needs "
            f"GEOS >= {required} for make_valid(method='structure'). Install a "
            "Shapely wheel built against a newer GEOS. There is no fallback."
        )


# Checked on import, so the API, the seed scripts and any worker all fail at
# startup rather than on the first geometry they repair.
ensure_geos_supports_structure()


class InvalidGeometryError(DomainError):
    """A geometry that cannot be stored: wrong type, malformed, or unrepairable."""

    def __init__(self, reason: str, **context: object) -> None:
        """Record why the geometry was rejected, plus safe diagnostic context."""
        super().__init__(reason)
        self.reason = reason
        self.context = context

    def as_dict(self) -> dict[str, object]:
        """Return the rejection reason and context for the response body."""
        return {"error": type(self).__name__, "reason": self.reason, **self.context}


class GeoJSONTooLargeError(DomainError):
    """A GeoJSON payload over the configured size budget."""

    def __init__(self, size_bytes: int, limit_bytes: int) -> None:
        """Record the offending size and the limit it exceeded."""
        super().__init__(f"GeoJSON payload of {size_bytes} bytes exceeds {limit_bytes}")
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes

    def as_dict(self) -> dict[str, object]:
        """Return the size and the limit for the response body."""
        return {
            "error": type(self).__name__,
            "size_bytes": self.size_bytes,
            "limit_bytes": self.limit_bytes,
        }


def max_geojson_bytes() -> int:
    """Return the payload size budget in bytes."""
    return get_settings().geo_max_geojson_size_kb * 1024


def ensure_geojson_size(size_bytes: int) -> None:
    """Reject a payload by its size, before anything tries to deserialize it."""
    limit = max_geojson_bytes()
    if size_bytes > limit:
        raise GeoJSONTooLargeError(size_bytes, limit)


def _check_position(position: object) -> None:
    if not isinstance(position, list | tuple) or len(position) < 2:
        raise InvalidGeometryError("each position must be [longitude, latitude]")
    lon, lat = position[0], position[1]
    for value in (lon, lat):
        # bool is an int subclass; a `true` coordinate is malformed input.
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise InvalidGeometryError("coordinates must be numbers")
        if not math.isfinite(value):
            raise InvalidGeometryError("coordinates must be finite numbers")
    if not LON_RANGE[0] <= lon <= LON_RANGE[1]:
        raise InvalidGeometryError("longitude out of range", longitude=lon)
    if not LAT_RANGE[0] <= lat <= LAT_RANGE[1]:
        raise InvalidGeometryError("latitude out of range", latitude=lat)


def _count_and_check_polygon(rings: object, budget: int) -> int:
    """Validate one polygon's rings and return how many positions it holds."""
    if not isinstance(rings, list) or not rings:
        raise InvalidGeometryError("a polygon needs at least one ring")
    count = 0
    for ring in rings:
        if not isinstance(ring, list):
            raise InvalidGeometryError("a ring must be a list of positions")
        count += len(ring)
        # Checked before walking the ring, so an oversized payload is refused
        # without iterating over it.
        if count > budget:
            raise InvalidGeometryError("too many vertices", limit=budget)
        for position in ring:
            _check_position(position)
    return count


def count_vertices(data: dict[str, Any]) -> int:
    """Return the number of positions in a Polygon or MultiPolygon GeoJSON."""
    budget = get_settings().geo_max_polygon_vertices
    coordinates = data.get("coordinates")
    if data.get("type") == "Polygon":
        return _count_and_check_polygon(coordinates, budget)

    if not isinstance(coordinates, list) or not coordinates:
        raise InvalidGeometryError("a multipolygon needs at least one polygon")
    total = 0
    for polygon in coordinates:
        total += _count_and_check_polygon(polygon, budget - total)
    return total


@dataclass(frozen=True)
class ValidatedGeometry:
    """A geometry that passed validation, plus what validation had to do to it."""

    geometry: BaseGeometry
    """Valid Polygon or MultiPolygon: what would be stored."""
    vertex_count: int
    repaired: bool
    """True when the input was invalid and ``make_valid`` rebuilt it."""
    reference_geometry: BaseGeometry | None
    """What the area *before* the repair is measured on; None if not repaired."""
    self_intersecting: bool
    """True when a ring of the input crosses itself (see has_self_crossing_ring)."""


def validate_geojson_geometry(data: dict[str, Any]) -> BaseGeometry:
    """Parse and validate an untrusted GeoJSON geometry.

    Shorthand for :func:`inspect_geojson_geometry` when the caller does not
    need to know whether the geometry was repaired.
    """
    return inspect_geojson_geometry(data).geometry


def inspect_geojson_geometry(data: dict[str, Any]) -> ValidatedGeometry:
    """Parse and validate an untrusted GeoJSON geometry, reporting any repair.

    Returns a valid, non-empty Polygon or MultiPolygon. An invalid geometry is
    repaired with ``make_valid(method="structure")``, which dissolves
    overlapping parts into their union instead of punching a hole where they
    overlap. The repair may turn a MultiPolygon into a Polygon; anything that
    is not polygonal afterwards is rejected. A geometry is never returned
    invalid.

    Whether the repair may be accepted without asking is not decided here:
    that needs areas in square metres, measured by the database. See
    :func:`assess_repair`.
    """
    if not isinstance(data, dict):
        raise InvalidGeometryError("geometry must be a GeoJSON object")
    geometry_type = data.get("type")
    if geometry_type not in ALLOWED_GEOMETRY_TYPES:
        raise InvalidGeometryError(
            "only Polygon and MultiPolygon are accepted",
            received=str(geometry_type),
        )

    vertices = count_vertices(data)

    try:
        geometry = cast(BaseGeometry, shape(data))
    except (ValueError, TypeError, AttributeError, shapely.errors.ShapelyError) as exc:
        raise InvalidGeometryError("malformed geometry") from exc

    if geometry.is_empty:
        raise InvalidGeometryError("geometry is empty")

    if geometry.is_valid:
        return ValidatedGeometry(
            geometry=geometry,
            vertex_count=vertices,
            repaired=False,
            reference_geometry=None,
            self_intersecting=False,
        )

    # Inspected on the input, before repairing: the repaired result no longer
    # shows which rings crossed themselves.
    self_intersecting = has_self_crossing_ring(geometry)
    fixed = cast(BaseGeometry, shapely.make_valid(geometry, method="structure"))
    if fixed.is_empty or fixed.geom_type not in ALLOWED_GEOMETRY_TYPES:
        raise InvalidGeometryError(
            "geometry is invalid and its repair is not a polygon",
            reason_detail=str(shapely.is_valid_reason(geometry)),
            repaired_type=str(fixed.geom_type),
        )
    if not fixed.is_valid:  # pragma: no cover - make_valid guarantees validity
        raise InvalidGeometryError("geometry could not be made valid")

    return ValidatedGeometry(
        geometry=fixed,
        vertex_count=vertices,
        repaired=True,
        reference_geometry=repair_reference_geometry(geometry),
        self_intersecting=self_intersecting,
    )


def _polygon_parts(geometry: BaseGeometry) -> list[Polygon]:
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    return [cast(Polygon, geometry)]


def has_self_crossing_ring(geometry: BaseGeometry) -> bool:
    """Return whether any ring of the input, exterior or interior, crosses itself.

    A crossing ring is ambiguous: the system cannot tell whether the person
    meant the outer contour or the pieces the crossing produces. Measured per
    ring, by noding the ring alone and counting the areas it encloses: a
    crossing ring (a bowtie, a figure eight) encloses two or more, while a
    ring that merely doubles back on itself (a spike) still encloses one.

    Overlapping parts of a MultiPolygon are not a crossing: each part's rings
    are fine on their own, and their union is the obvious intent.
    """
    for part in _polygon_parts(geometry):
        for ring in (part.exterior, *part.interiors):
            if ring.is_simple:
                continue
            faces = shapely.make_valid(Polygon(ring.coords), method="linework")
            enclosed = [
                piece
                for piece in shapely.get_parts(faces)
                if isinstance(piece, Polygon) and piece.area > 0
            ]
            if len(enclosed) > 1:
                return True
    return False


def repair_reference_geometry(geometry: BaseGeometry) -> BaseGeometry:
    """Área de referencia para medir el efecto de la reparación.

    Supuesto: cuando las partes de un MultiPolygon se solapan, la intención es
    la unión, no un hueco. En GeoJSON un hueco se expresa como anillo interior
    de un polígono, no como dos partes superpuestas; quien quiso un hueco lo
    dibujó como hueco.

    Consecuencia: si este supuesto no se cumple para algún origen de datos
    futuro (un exportador que represente huecos con partes superpuestas), esta
    función mediría mal y la reparación se aceptaría sin aviso.

    Returns the union of the parts when every part is valid on its own. When
    some part is broken in itself there is no meaningful "before" to rebuild,
    and the raw input is returned: its area is then only a heuristic (see the
    caveat in :func:`assess_repair`).
    """
    parts = _polygon_parts(geometry)
    if len(parts) > 1 and all(part.is_valid for part in parts):
        return cast(BaseGeometry, shapely.union_all(parts))
    return geometry


class RepairVerdict(StrEnum):
    """What an automatic repair's effect on the area allows."""

    WITHIN_THRESHOLD = "WITHIN_THRESHOLD"
    ABOVE_THRESHOLD = "AREA_CHANGE_ABOVE_THRESHOLD"
    NOT_MEASURABLE_ZERO_AREA = "NOT_MEASURABLE_ZERO_AREA"
    NOT_MEASURABLE_SELF_INTERSECTION = "NOT_MEASURABLE_SELF_INTERSECTION"


_NOT_MEASURABLE: Final = frozenset(
    {
        RepairVerdict.NOT_MEASURABLE_ZERO_AREA,
        RepairVerdict.NOT_MEASURABLE_SELF_INTERSECTION,
    }
)

# A reference area this small next to the repaired one makes the ratio
# meaningless (a symmetric bowtie cancels to 0; a nearly symmetric one to a
# sliver). Under 0.1% of the result, it is treated as zero.
ZERO_REFERENCE_FRACTION: Final = 1e-3


@dataclass(frozen=True)
class RepairAssessment:
    """How much an automatic repair changed the area, and what that allows."""

    area_before_m2: float
    area_after_m2: float
    verdict: RepairVerdict
    max_ratio: float
    ignore_below_m2: float

    @property
    def change_m2(self) -> float:
        """Return the signed area change: after minus before."""
        return self.area_after_m2 - self.area_before_m2

    @property
    def change_ratio(self) -> float | None:
        """Return the signed relative change, or None when it measures nothing."""
        if self.verdict in _NOT_MEASURABLE:
            return None
        return self.change_m2 / self.area_before_m2

    @property
    def measurable(self) -> bool:
        """Return whether the area change means anything."""
        return self.verdict not in _NOT_MEASURABLE

    @property
    def accepted_automatically(self) -> bool:
        """Return whether the repair may be stored without explicit confirmation."""
        return self.verdict is RepairVerdict.WITHIN_THRESHOLD

    def as_details(self) -> dict[str, object]:
        """Return the figures for audit details and API payloads. No geometry."""
        ratio = self.change_ratio
        return {
            "area_before_m2": round(self.area_before_m2, 2),
            "area_after_m2": round(self.area_after_m2, 2),
            "area_change_m2": round(self.change_m2, 2),
            "area_change_ratio": None if ratio is None else round(ratio, 6),
            "threshold_ratio": self.max_ratio,
            "ignore_below_m2": self.ignore_below_m2,
            "reason": self.verdict.value,
        }


def assess_repair(
    *,
    area_before_m2: float,
    area_after_m2: float,
    self_intersecting: bool,
    max_ratio: float,
    ignore_below_m2: float,
) -> RepairAssessment:
    """Decide whether a repair may be accepted without explicit confirmation.

    CAVEAT: ``area_before_m2`` is measured on a geometry that was invalid, and
    ST_Area over an invalid geometry is not reliable: overlapping zones can be
    counted twice or cancel out. repair_reference_geometry() removes the
    common case (overlapping parts), but what remains is a safety heuristic,
    not a measurement. Do not read a WITHIN_THRESHOLD verdict as a guarantee
    that the stored shape is what the person drew.

    Two situations make the ratio meaningless, and both demand confirmation
    whatever the numbers say:

    - The reference area is zero or close to it (a symmetric bowtie cancels
      out): NOT_MEASURABLE_ZERO_AREA.
    - A ring crosses itself: the reference area is then positive but
      arbitrary (an asymmetric bowtie), and the ratio may fall under the
      threshold by coincidence: NOT_MEASURABLE_SELF_INTERSECTION.

    Otherwise the repair is accepted when the change is under EITHER bound:
    the absolute floor forgives digitising noise on small geometries, the
    ratio protects large ones. It is refused only when both are exceeded.
    """
    if area_before_m2 <= area_after_m2 * ZERO_REFERENCE_FRACTION:
        verdict = RepairVerdict.NOT_MEASURABLE_ZERO_AREA
    elif self_intersecting:
        verdict = RepairVerdict.NOT_MEASURABLE_SELF_INTERSECTION
    else:
        change = abs(area_after_m2 - area_before_m2)
        within = change < ignore_below_m2 or change / area_before_m2 < max_ratio
        verdict = (
            RepairVerdict.WITHIN_THRESHOLD if within else RepairVerdict.ABOVE_THRESHOLD
        )
    return RepairAssessment(
        area_before_m2=area_before_m2,
        area_after_m2=area_after_m2,
        verdict=verdict,
        max_ratio=max_ratio,
        ignore_below_m2=ignore_below_m2,
    )


def geometry_to_geojson(geometry: BaseGeometry) -> dict[str, Any]:
    """Return a JSON-safe GeoJSON dict (lists, not tuples)."""
    return cast(dict[str, Any], json.loads(json.dumps(mapping(geometry))))


def geojson_to_wkb(geometry: BaseGeometry) -> WKBElement:
    """Return a WKB element for GeoAlchemy2, bound as a parameter, never interpolated."""
    return from_shape(geometry, srid=get_settings().geo_srid)


def wkb_to_geometry(element: WKBElement) -> BaseGeometry:
    """Return the Shapely geometry stored in a WKB element."""
    return cast(BaseGeometry, to_shape(element))


_GEOJSON_KEYS: Final = frozenset({"coordinates", "geometries", "geometry", "features"})


def contains_geojson(value: object) -> bool:
    """Return whether ``value`` carries a GeoJSON geometry at any depth.

    Used to keep full geometries out of places that must not store them,
    such as ``audit_log.details``: a key named like a GeoJSON member is
    enough to refuse, whatever its content.
    """
    if isinstance(value, dict):
        return any(
            key in _GEOJSON_KEYS or contains_geojson(item) for key, item in value.items()
        )
    if isinstance(value, list | tuple):
        return any(contains_geojson(item) for item in value)
    return False


def is_within_chile_bbox(geometry: BaseGeometry) -> bool:
    """Return whether the geometry lies inside Chile's approximate bounding box.

    Used to warn, never to reject: the system must not assume it will only ever
    operate in Chile.
    """
    return bool(box(*CHILE_BBOX).covers(geometry))
