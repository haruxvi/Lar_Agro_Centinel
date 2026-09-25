"""Validation of untrusted GeoJSON before anything is stored."""

from __future__ import annotations

import copy
from typing import Any

import pytest
import shapely
from shapely.geometry import shape
from shapely.ops import unary_union

from app.shared.config import get_settings
from app.shared.geo import (
    GeoJSONTooLargeError,
    InvalidGeometryError,
    RepairAssessment,
    RepairVerdict,
    assess_repair,
    count_vertices,
    ensure_geojson_size,
    ensure_geos_supports_structure,
    geojson_to_wkb,
    geometry_to_geojson,
    has_self_crossing_ring,
    inspect_geojson_geometry,
    is_within_chile_bbox,
    max_geojson_bytes,
    validate_geojson_geometry,
    wkb_to_geometry,
)
from tests.fixtures import geometries as g

# --- accepted geometries ------------------------------------------------------


def test_a_valid_polygon_parses() -> None:
    geometry = validate_geojson_geometry(g.SQUARE)
    assert geometry.geom_type == "Polygon"
    assert geometry.is_valid
    assert not geometry.is_empty


def test_a_polygon_with_a_hole_parses() -> None:
    geometry = validate_geojson_geometry(g.POLYGON_WITH_HOLE)
    assert geometry.geom_type == "Polygon"
    assert len(geometry.interiors) == 1


def test_a_valid_multipolygon_parses() -> None:
    geometry = validate_geojson_geometry(g.MULTIPOLYGON)
    assert geometry.geom_type == "MultiPolygon"
    assert len(geometry.geoms) == 2


def test_the_input_is_not_mutated() -> None:
    original = copy.deepcopy(g.SQUARE)
    validate_geojson_geometry(g.SQUARE)
    assert original == g.SQUARE


# --- rejected types -----------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "received"),
    [
        (g.POINT, "Point"),
        (g.LINESTRING, "LineString"),
        (g.GEOMETRY_COLLECTION, "GeometryCollection"),
    ],
)
def test_types_other_than_polygon_and_multipolygon_are_rejected(
    data: dict[str, Any], received: str
) -> None:
    with pytest.raises(InvalidGeometryError) as exc_info:
        validate_geojson_geometry(data)
    assert exc_info.value.as_dict()["received"] == received


@pytest.mark.parametrize(
    "data",
    [{}, {"type": None}, {"type": "Feature"}, {"type": "polygon"}],
)
def test_missing_or_unknown_types_are_rejected(data: dict[str, Any]) -> None:
    with pytest.raises(InvalidGeometryError):
        validate_geojson_geometry(data)


def test_a_non_object_is_rejected() -> None:
    with pytest.raises(InvalidGeometryError):
        validate_geojson_geometry([1, 2, 3])  # type: ignore[arg-type]


# --- vertex budget ------------------------------------------------------------


def test_the_vertex_count_is_exact() -> None:
    assert count_vertices(g.SQUARE) == 5
    assert count_vertices(g.MULTIPOLYGON) == 10


def test_a_polygon_at_the_vertex_limit_is_accepted() -> None:
    limit = get_settings().geo_max_polygon_vertices
    # many_vertices(n) closes the ring, so it holds n + 1 positions.
    validate_geojson_geometry(g.many_vertices(limit - 1))


def test_a_polygon_over_the_vertex_limit_is_rejected() -> None:
    limit = get_settings().geo_max_polygon_vertices
    with pytest.raises(InvalidGeometryError, match="too many vertices"):
        validate_geojson_geometry(g.many_vertices(limit + 10))


def test_the_vertex_limit_applies_across_multipolygon_parts() -> None:
    # Each part is under the limit on its own; together they are not.
    limit = get_settings().geo_max_polygon_vertices
    part = g.many_vertices(limit // 2 + 10)
    data = {
        "type": "MultiPolygon",
        "coordinates": [part["coordinates"], part["coordinates"]],
    }
    with pytest.raises(InvalidGeometryError, match="too many vertices"):
        validate_geojson_geometry(data)


# --- coordinates --------------------------------------------------------------


@pytest.mark.parametrize(
    "position",
    [
        [181.0, -34.0],
        [-181.0, -34.0],
        [-71.0, 91.0],
        [-71.0, -91.0],
    ],
)
def test_out_of_range_coordinates_are_rejected(position: list[float]) -> None:
    data = copy.deepcopy(g.SQUARE)
    data["coordinates"][0][1] = position
    with pytest.raises(InvalidGeometryError, match="out of range"):
        validate_geojson_geometry(data)


def test_swapped_coordinates_pass_range_checks_but_leave_the_chile_bbox() -> None:
    # For Chile, swapping longitude and latitude stays within both ranges
    # (-71 is a valid latitude), so range validation cannot catch it. The swap
    # lands near Antarctica, which is what the bounding-box warning exists for.
    data = copy.deepcopy(g.SQUARE)
    data["coordinates"][0] = [[lat, lon] for lon, lat in data["coordinates"][0]]

    geometry = validate_geojson_geometry(data)
    assert is_within_chile_bbox(geometry) is False


@pytest.mark.parametrize(
    "position",
    [
        ["-71", "-34"],
        [True, -34.0],
        [float("nan"), -34.0],
        [float("inf"), -34.0],
        [-71.0],
    ],
)
def test_malformed_positions_are_rejected(position: list[Any]) -> None:
    data = copy.deepcopy(g.SQUARE)
    data["coordinates"][0][1] = position
    with pytest.raises(InvalidGeometryError):
        validate_geojson_geometry(data)


# --- validity and repair ------------------------------------------------------


def test_an_empty_polygon_is_rejected() -> None:
    with pytest.raises(InvalidGeometryError):
        validate_geojson_geometry(g.EMPTY_POLYGON)


def test_a_valid_geometry_is_not_repaired() -> None:
    validated = inspect_geojson_geometry(g.POLYGON_WITH_HOLE)
    assert (validated.repaired, validated.reference_geometry) == (False, None)
    assert validated.self_intersecting is False


def test_overlapping_parts_are_repaired_into_their_union() -> None:
    # "structure", verified by its result: the union, not a hole in the overlap.
    parts = [shape(part) for part in _parts(g.OVERLAPPING_MULTIPOLYGON)]
    validated = inspect_geojson_geometry(g.OVERLAPPING_MULTIPOLYGON)

    assert validated.repaired
    assert validated.geometry.geom_type == "Polygon"
    assert validated.geometry.equals(unary_union(parts))
    assert validated.self_intersecting is False


def test_overlap_changes_0_percent_with_structure_and_25_with_linework() -> None:
    # The case that motivated the change, measured both ways against the
    # reference area (the union of the parts).
    raw = shape(g.OVERLAPPING_MULTIPOLYGON)
    validated = inspect_geojson_geometry(g.OVERLAPPING_MULTIPOLYGON)
    assert validated.reference_geometry is not None
    reference = validated.reference_geometry.area

    linework = shapely.make_valid(raw, method="linework")
    assert (validated.geometry.area - reference) / reference == pytest.approx(0.0)
    assert (linework.area - raw.area) / raw.area == pytest.approx(-0.25)


def test_the_reference_of_a_single_broken_polygon_is_the_input_itself() -> None:
    validated = inspect_geojson_geometry(g.SPIKE)
    assert validated.reference_geometry is not None
    assert validated.reference_geometry.equals(shape(g.SPIKE))


def test_a_spike_is_repaired_without_changing_the_area() -> None:
    validated = inspect_geojson_geometry(g.SPIKE)
    assert validated.repaired
    assert validated.geometry.geom_type == "Polygon"
    assert validated.self_intersecting is False
    assert validated.geometry.area == pytest.approx(shape(g.SPIKE).area)


@pytest.mark.parametrize("data", [g.SELF_INTERSECTING, g.ASYMMETRIC_BOWTIE])
def test_a_bowtie_is_repaired_but_flagged_as_self_intersecting(
    data: dict[str, Any],
) -> None:
    validated = inspect_geojson_geometry(data)
    assert validated.repaired
    assert validated.geometry.geom_type == "MultiPolygon"
    assert validated.self_intersecting is True


def test_the_symmetric_bowtie_cancels_to_zero() -> None:
    assert shape(g.SELF_INTERSECTING).area == 0


def test_a_crossing_hole_ring_is_self_intersecting() -> None:
    shell = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
    bowtie_hole = [[2, 2], [4, 4], [4, 2], [2, 4], [2, 2]]
    assert has_self_crossing_ring(
        shape({"type": "Polygon", "coordinates": [shell, bowtie_hole]})
    )


def test_a_protruding_hole_is_not_a_crossing() -> None:
    data = g.protruding_hole(side_m=100, hole_height_m=10, inside_m=1, outside_m=1)
    validated = inspect_geojson_geometry(data)
    assert validated.repaired
    assert validated.self_intersecting is False


@pytest.mark.parametrize(
    "data",
    [
        g.SQUARE,
        g.POLYGON_WITH_HOLE,
        g.MULTIPOLYGON,
        g.OVERLAPPING_MULTIPOLYGON,
        g.SPIKE,
        g.SELF_INTERSECTING,
        g.ASYMMETRIC_BOWTIE,
        g.protruding_hole(side_m=100, hole_height_m=10, inside_m=1, outside_m=1),
    ],
)
def test_whatever_is_returned_is_valid(data: dict[str, Any]) -> None:
    # The only guarantee that matters: nothing invalid ever leaves this function.
    assert validate_geojson_geometry(data).is_valid


def _parts(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"type": "Polygon", "coordinates": coords} for coords in data["coordinates"]]


# --- GEOS version -------------------------------------------------------------


def test_startup_fails_clearly_with_an_old_geos() -> None:
    with pytest.raises(RuntimeError, match=r"GEOS 3\.9\.4 is too old.*>= 3\.10\.0"):
        ensure_geos_supports_structure((3, 9, 4))


def test_the_installed_geos_is_supported() -> None:
    ensure_geos_supports_structure()  # does not raise
    assert shapely.geos_version >= (3, 10, 0)


# --- repair assessment --------------------------------------------------------

THRESHOLDS = {"max_ratio": 0.01, "ignore_below_m2": 50.0}


def _assess(before: float, after: float, *, crossing: bool = False) -> RepairAssessment:
    return assess_repair(
        area_before_m2=before,
        area_after_m2=after,
        self_intersecting=crossing,
        **THRESHOLDS,
    )


@pytest.mark.parametrize(
    ("before", "after", "verdict"),
    [
        # Absolute floor: 10 m2 on a 480 m2 parcel is 2%, but under 50 m2.
        (480.0, 490.0, RepairVerdict.WITHIN_THRESHOLD),
        # Ratio: 200 m2 on ~45 ha is over the floor but 0.04%.
        (450_000.0, 450_200.0, RepairVerdict.WITHIN_THRESHOLD),
        # Both exceeded: 20,000 m2 on ~97 ha is 2%.
        (971_262.0, 991_288.0, RepairVerdict.ABOVE_THRESHOLD),
        # A loss counts the same as a gain.
        (1_000_000.0, 950_000.0, RepairVerdict.ABOVE_THRESHOLD),
    ],
)
def test_either_bound_forgives_and_only_both_reject(
    before: float, after: float, verdict: RepairVerdict
) -> None:
    assert _assess(before, after).verdict is verdict


def test_a_zero_reference_area_is_not_measurable() -> None:
    assessment = _assess(0.0, 508_585.0)
    assert assessment.verdict is RepairVerdict.NOT_MEASURABLE_ZERO_AREA
    assert assessment.change_ratio is None
    assert not assessment.accepted_automatically


def test_a_near_zero_reference_area_is_not_measurable() -> None:
    # A nearly symmetric bowtie: positive, but a sliver of the result.
    assert _assess(100.0, 500_000.0).verdict is RepairVerdict.NOT_MEASURABLE_ZERO_AREA


def test_a_crossing_ring_is_not_measurable_even_under_the_threshold() -> None:
    # The asymmetric bowtie measured in PostGIS: +400 m2 on ~50 ha, 0.08%. By
    # the numbers alone it would pass; the crossing makes them meaningless.
    assert _assess(498_384.2, 498_784.2).accepted_automatically
    crossing = _assess(498_384.2, 498_784.2, crossing=True)
    assert crossing.verdict is RepairVerdict.NOT_MEASURABLE_SELF_INTERSECTION
    assert crossing.as_details()["area_change_ratio"] is None


def test_the_details_carry_the_figures_and_no_geometry() -> None:
    details = _assess(1_000_000.0, 950_000.0).as_details()
    assert details == {
        "area_before_m2": 1_000_000.0,
        "area_after_m2": 950_000.0,
        "area_change_m2": -50_000.0,
        "area_change_ratio": -0.05,
        "threshold_ratio": 0.01,
        "ignore_below_m2": 50.0,
        "reason": "AREA_CHANGE_ABOVE_THRESHOLD",
    }


# --- conversions --------------------------------------------------------------


def test_geojson_roundtrip_is_lossless() -> None:
    geometry = validate_geojson_geometry(g.SQUARE)
    assert validate_geojson_geometry(geometry_to_geojson(geometry)).equals(geometry)


def test_geojson_output_uses_lists_not_tuples() -> None:
    output = geometry_to_geojson(validate_geojson_geometry(g.SQUARE))
    assert isinstance(output["coordinates"], list)
    assert isinstance(output["coordinates"][0][0], list)


def test_wkb_roundtrip_preserves_geometry_and_srid() -> None:
    geometry = validate_geojson_geometry(g.MULTIPOLYGON)
    element = geojson_to_wkb(geometry)
    assert element.srid == get_settings().geo_srid
    assert wkb_to_geometry(element).equals(geometry)


# --- Chile bounding box (warning, never rejection) ----------------------------


def test_a_geometry_in_chile_is_inside_the_bbox() -> None:
    assert is_within_chile_bbox(validate_geojson_geometry(g.SQUARE)) is True


def test_a_geometry_outside_chile_is_flagged_not_rejected() -> None:
    geometry = validate_geojson_geometry(g.OUTSIDE_CHILE)  # does not raise
    assert is_within_chile_bbox(geometry) is False


def test_rapa_nui_counts_as_chile() -> None:
    rapa_nui = g.square(lon=-109.40, lat=-27.15)
    assert is_within_chile_bbox(validate_geojson_geometry(rapa_nui)) is True


# --- payload size -------------------------------------------------------------


def test_a_payload_within_the_budget_passes() -> None:
    ensure_geojson_size(max_geojson_bytes())


def test_a_payload_over_the_budget_is_rejected_before_parsing() -> None:
    with pytest.raises(GeoJSONTooLargeError) as exc_info:
        ensure_geojson_size(max_geojson_bytes() + 1)
    assert exc_info.value.as_dict()["limit_bytes"] == max_geojson_bytes()


def test_rejections_explain_themselves_without_leaking_input() -> None:
    with pytest.raises(InvalidGeometryError) as exc_info:
        validate_geojson_geometry(g.POINT)
    payload = exc_info.value.as_dict()
    assert payload["error"] == "InvalidGeometryError"
    assert "coordinates" not in payload
