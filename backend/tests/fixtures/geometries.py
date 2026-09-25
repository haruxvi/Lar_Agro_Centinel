"""GeoJSON geometries reused across geospatial tests.

Coordinates sit in the Colchagua valley (around Santa Cruz, O'Higgins) so the
fixtures stay inside Chile's bounding box unless a test says otherwise.
"""

from __future__ import annotations

from typing import Any

# Anchor point near Santa Cruz, Colchagua.
BASE_LON = -71.3650
BASE_LAT = -34.6400

# ~0.01 degrees is roughly 910 m of longitude and 1.1 km of latitude here.
STEP = 0.01


def square(
    lon: float = BASE_LON, lat: float = BASE_LAT, size: float = STEP
) -> dict[str, Any]:
    """Return a closed square Polygon with its south-west corner at (lon, lat)."""
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [lon, lat],
                [lon + size, lat],
                [lon + size, lat + size],
                [lon, lat + size],
                [lon, lat],
            ]
        ],
    }


SQUARE = square()

# A polygon with a hole: an outer ring and an interior courtyard.
POLYGON_WITH_HOLE: dict[str, Any] = {
    "type": "Polygon",
    "coordinates": [
        [
            [BASE_LON, BASE_LAT],
            [BASE_LON + 0.02, BASE_LAT],
            [BASE_LON + 0.02, BASE_LAT + 0.02],
            [BASE_LON, BASE_LAT + 0.02],
            [BASE_LON, BASE_LAT],
        ],
        [
            [BASE_LON + 0.005, BASE_LAT + 0.005],
            [BASE_LON + 0.015, BASE_LAT + 0.005],
            [BASE_LON + 0.015, BASE_LAT + 0.015],
            [BASE_LON + 0.005, BASE_LAT + 0.015],
            [BASE_LON + 0.005, BASE_LAT + 0.005],
        ],
    ],
}

# Two disjoint parcels: the common case of a predio split by a road.
MULTIPOLYGON: dict[str, Any] = {
    "type": "MultiPolygon",
    "coordinates": [
        square()["coordinates"],
        square(lon=BASE_LON + 0.03)["coordinates"],
    ],
}

# A symmetric "bowtie": the ring crosses itself in the middle. Its two halves
# have opposite orientation, so its raw area cancels to exactly zero.
SELF_INTERSECTING: dict[str, Any] = {
    "type": "Polygon",
    "coordinates": [
        [
            [BASE_LON, BASE_LAT],
            [BASE_LON + STEP, BASE_LAT + STEP],
            [BASE_LON + STEP, BASE_LAT],
            [BASE_LON, BASE_LAT + STEP],
            [BASE_LON, BASE_LAT],
        ]
    ],
}

# A spike: the ring runs out and back along the same line. Invalid, but not a
# crossing: the ring still encloses a single area. make_valid("structure")
# drops the spike and the area does not change.
SPIKE: dict[str, Any] = {
    "type": "Polygon",
    "coordinates": [
        [
            [BASE_LON, BASE_LAT],
            [BASE_LON + STEP, BASE_LAT],
            [BASE_LON + STEP, BASE_LAT + STEP],
            [BASE_LON + STEP / 2, BASE_LAT + STEP],
            [BASE_LON + STEP / 2, BASE_LAT + 2 * STEP],
            [BASE_LON + STEP / 2, BASE_LAT + STEP],
            [BASE_LON, BASE_LAT + STEP],
            [BASE_LON, BASE_LAT],
        ]
    ],
}

# Two parts overlapping diagonally. Invalid as a MultiPolygon. The old
# "linework" repair punched a hole in the overlap (0.0008 -> 0.0006 deg2, -25%);
# "structure" returns their union, a single Polygon of 0.0007 deg2.
OVERLAPPING_MULTIPOLYGON: dict[str, Any] = {
    "type": "MultiPolygon",
    "coordinates": [
        square(size=0.02)["coordinates"],
        square(lon=BASE_LON + 0.01, lat=BASE_LAT + 0.01, size=0.02)["coordinates"],
    ],
}


def many_vertices(count: int) -> dict[str, Any]:
    """Return a valid circle-like Polygon with ``count`` vertices."""
    import math

    ring = [
        [
            BASE_LON + 0.01 * math.cos(2 * math.pi * i / count),
            BASE_LAT + 0.01 * math.sin(2 * math.pi * i / count),
        ]
        for i in range(count)
    ]
    ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


POINT: dict[str, Any] = {"type": "Point", "coordinates": [BASE_LON, BASE_LAT]}

LINESTRING: dict[str, Any] = {
    "type": "LineString",
    "coordinates": [[BASE_LON, BASE_LAT], [BASE_LON + STEP, BASE_LAT + STEP]],
}

GEOMETRY_COLLECTION: dict[str, Any] = {
    "type": "GeometryCollection",
    "geometries": [SQUARE, POINT],
}

EMPTY_POLYGON: dict[str, Any] = {"type": "Polygon", "coordinates": []}

# Somewhere in the Pampas: a perfectly good polygon, just not in Chile.
OUTSIDE_CHILE = square(lon=-62.0, lat=-35.0)


# An asymmetric bowtie: the crossing sits near one end, so one lobe is tiny.
# Its raw area no longer cancels (big lobe minus small lobe), and the repair
# changes it by only ~0.08%: a ratio that means nothing but would pass 1%.
ASYMMETRIC_BOWTIE: dict[str, Any] = {
    "type": "Polygon",
    "coordinates": [
        [
            [BASE_LON + x * STEP, BASE_LAT + y * STEP]
            for x, y in [(0, 0), (1, 1), (1, 0), (0, 0.02), (0, 0)]
        ]
    ],
}

# Metres to degrees around BASE_LAT (34.64 S), for geometries sized in metres.
DEG_PER_M_LON = 1 / 91_600
DEG_PER_M_LAT = 1 / 110_900


def protruding_hole(
    side_m: float,
    hole_height_m: float,
    inside_m: float,
    outside_m: float,
    *,
    lon: float = BASE_LON,
    lat: float = BASE_LAT,
) -> dict[str, Any]:
    """Return a square predio whose hole sticks out through its eastern edge.

    Invalid (a hole must lie inside its shell), with no ring crossing itself.
    The repair keeps the shell minus the part of the hole that is inside it, so
    the area grows by exactly the part that sticks out:
    ``hole_height_m * outside_m`` square metres.
    """

    def ring(points: list[tuple[float, float]]) -> list[list[float]]:
        return [[lon + x * DEG_PER_M_LON, lat + y * DEG_PER_M_LAT] for x, y in points]

    y0 = side_m / 2 - hole_height_m / 2
    y1 = y0 + hole_height_m
    x0, x1 = side_m - inside_m, side_m + outside_m
    shell = [(0, 0), (side_m, 0), (side_m, side_m), (0, side_m), (0, 0)]
    hole = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return {"type": "Polygon", "coordinates": [ring(shell), ring(hole)]}
