"""Repository reads, writes and spatial queries against real PostGIS."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from shapely.geometry.base import BaseGeometry
from sqlalchemy.orm import Session

from app.modules.predios.repository import LoteRepository, PredioRepository
from app.shared.geo import validate_geojson_geometry
from app.shared.pagination import MAX_PAGE_SIZE, PageParams
from tests.fixtures import geometries as g
from tests.modules.predios.conftest import MakeLote, MakePredio, MakeUser

# Metres to degrees around the fixtures' latitude (34.64 S).
DEG_PER_M_LON = 1 / 91_600
DEG_PER_M_LAT = 1 / 110_900

HALF = g.STEP / 2


@pytest.fixture
def predios(db_session: Session) -> PredioRepository:
    return PredioRepository(db_session)


@pytest.fixture
def lotes(db_session: Session) -> LoteRepository:
    return LoteRepository(db_session)


def _shape(geojson: dict[str, Any]) -> BaseGeometry:
    return validate_geojson_geometry(geojson)


# --- computations -------------------------------------------------------------


def test_area_of_a_known_square(predios: PredioRepository) -> None:
    assert predios.compute_area_m2(_shape(g.SQUARE)) == pytest.approx(1_017_000, rel=0.01)


def test_centroid_of_a_known_square(predios: PredioRepository) -> None:
    centroid = predios.compute_centroid(_shape(g.SQUARE))
    assert centroid.x == pytest.approx(g.BASE_LON + HALF, abs=1e-9)
    assert centroid.y == pytest.approx(g.BASE_LAT + HALF, abs=1e-9)


# --- reads and writes ---------------------------------------------------------


def test_get_by_id_hides_soft_deleted_predios(
    predios: PredioRepository, make_predio: MakePredio
) -> None:
    predio = make_predio()
    predios.soft_delete(predio, predio.owner_user_id)

    assert predios.get_by_id(predio.id) is None
    stored = predios.get_by_id(predio.id, include_deleted=True)
    assert stored is not None
    assert stored.deleted_at is not None
    assert stored.deleted_by_user_id == predio.owner_user_id
    assert stored.is_active is False


def test_get_by_slug_is_scoped_to_the_owner(
    predios: PredioRepository, make_predio: MakePredio, make_user: MakeUser
) -> None:
    owner, stranger = make_user(), make_user()
    predio = make_predio(owner, slug="santa-elena")

    assert predios.get_by_slug(owner.id, "santa-elena") == predio
    assert predios.get_by_slug(stranger.id, "santa-elena") is None
    assert predios.slug_exists(owner.id, "santa-elena") is True


def test_update_applies_the_changes(
    predios: PredioRepository, make_predio: MakePredio
) -> None:
    predio = make_predio()
    predios.update(predio, {"name": "Viña Los Robles", "comuna": "Santa Cruz"})
    stored = predios.get_by_id(predio.id)
    assert stored is not None
    assert (stored.name, stored.comuna) == ("Viña Los Robles", "Santa Cruz")


def test_list_by_owner_only_returns_the_owners_live_predios(
    predios: PredioRepository, make_predio: MakePredio, make_user: MakeUser
) -> None:
    owner = make_user()
    kept = [make_predio(owner, name="A"), make_predio(owner, name="B")]
    deleted = make_predio(owner, name="C")
    predios.soft_delete(deleted, owner.id)
    make_predio(make_user(), name="D")

    page = predios.list_by_owner(owner.id, PageParams())
    assert page.items == kept
    assert page.total == 2


def test_list_by_owner_paginates(
    predios: PredioRepository, make_predio: MakePredio, make_user: MakeUser
) -> None:
    owner = make_user()
    created = [make_predio(owner, name=f"Predio {i}") for i in range(3)]

    first = predios.list_by_owner(owner.id, PageParams(page=1, page_size=2))
    second = predios.list_by_owner(owner.id, PageParams(page=2, page_size=2))

    assert first.total == second.total == 3
    assert first.pages == 2
    assert first.items + second.items == created


def test_list_by_owner_never_returns_more_than_the_maximum(
    predios: PredioRepository, make_user: MakeUser
) -> None:
    page = predios.list_by_owner(make_user().id, PageParams(page_size=10_000))
    assert page.page_size == MAX_PAGE_SIZE


def test_list_by_ids(predios: PredioRepository, make_predio: MakePredio) -> None:
    wanted = make_predio(name="A")
    make_predio(name="B")

    page = predios.list_by_ids([wanted.id, uuid.uuid4()], PageParams())
    assert page.items == [wanted]
    assert predios.list_by_ids([], PageParams()).total == 0


# --- spatial queries ----------------------------------------------------------


def test_finds_the_predio_containing_a_point(
    predios: PredioRepository, make_predio: MakePredio
) -> None:
    predio = make_predio()
    inside = predios.find_predios_containing_point(g.BASE_LON + HALF, g.BASE_LAT + HALF)
    outside = predios.find_predios_containing_point(g.BASE_LON - HALF, g.BASE_LAT - HALF)

    assert predio in inside
    assert predio not in outside


def test_a_soft_deleted_predio_contains_nothing(
    predios: PredioRepository, make_predio: MakePredio
) -> None:
    predio = make_predio()
    predios.soft_delete(predio, predio.owner_user_id)
    found = predios.find_predios_containing_point(g.BASE_LON + HALF, g.BASE_LAT + HALF)
    assert predio not in found


def test_finds_predios_within_a_radius_in_metres(
    predios: PredioRepository, make_predio: MakePredio
) -> None:
    predio = make_predio()
    # A point 500 m east of the centroid.
    lon = g.BASE_LON + HALF + 500 * DEG_PER_M_LON
    lat = g.BASE_LAT + HALF

    assert predio in predios.find_predios_within_radius(lon, lat, 1_000)
    assert predio not in predios.find_predios_within_radius(lon, lat, 300)


def test_finds_an_overlapping_predio_and_measures_the_overlap(
    predios: PredioRepository, make_predio: MakePredio
) -> None:
    existing = make_predio()
    # Shifted half a step north-east: shares a quarter of the square.
    candidate = _shape(g.square(lon=g.BASE_LON + HALF, lat=g.BASE_LAT + HALF))

    overlaps = dict(predios.find_overlapping_predios(candidate))
    assert existing in overlaps
    assert overlaps[existing] == pytest.approx(existing.area_m2 / 4, rel=0.01)


def test_predios_that_only_share_a_border_do_not_overlap(
    predios: PredioRepository, make_predio: MakePredio
) -> None:
    existing = make_predio()
    neighbour = _shape(g.square(lon=g.BASE_LON + g.STEP))

    assert existing not in dict(predios.find_overlapping_predios(neighbour))


def test_a_predio_does_not_overlap_itself_when_excluded(
    predios: PredioRepository, make_predio: MakePredio
) -> None:
    predio = make_predio()
    overlaps = predios.find_overlapping_predios(
        _shape(g.SQUARE), exclude_predio_id=predio.id
    )
    assert predio not in dict(overlaps)


def test_a_lote_inside_its_predio_is_contained(predios: PredioRepository) -> None:
    lote = _shape(g.square(size=HALF))
    assert predios.lote_within_predio(lote, _shape(g.SQUARE), tolerance_m=5.0)


def test_a_lote_slightly_outside_is_absorbed_by_the_tolerance(
    predios: PredioRepository,
) -> None:
    # Two metres past the western border: digitising noise, not an error.
    lote = _shape(g.square(lon=g.BASE_LON - 2 * DEG_PER_M_LON, size=HALF))
    assert predios.lote_within_predio(lote, _shape(g.SQUARE), tolerance_m=5.0)
    assert not predios.lote_within_predio(lote, _shape(g.SQUARE), tolerance_m=0.0)


def test_a_lote_well_outside_is_not_contained(predios: PredioRepository) -> None:
    lote = _shape(g.square(lon=g.BASE_LON - 50 * DEG_PER_M_LON, size=HALF))
    predio = _shape(g.SQUARE)

    assert not predios.lote_within_predio(lote, predio, tolerance_m=5.0)
    # ~50 m by ~554 m sticks out.
    assert predios.area_outside_m2(lote, predio) == pytest.approx(50 * 554, rel=0.05)


# --- lotes --------------------------------------------------------------------


def test_a_lote_is_only_found_within_its_own_predio(
    lotes: LoteRepository, make_predio: MakePredio, make_lote: MakeLote
) -> None:
    home, elsewhere = make_predio(), make_predio()
    lote = make_lote(home)

    assert lotes.get_in_predio(home.id, lote.id) == lote
    assert lotes.get_in_predio(elsewhere.id, lote.id) is None


def test_sibling_lotes_that_overlap_are_found(
    lotes: LoteRepository, make_predio: MakePredio, make_lote: MakeLote
) -> None:
    predio = make_predio()
    sibling = make_lote(predio)
    candidate = _shape(g.square(lon=g.BASE_LON + HALF / 2, size=HALF))

    overlaps = dict(lotes.find_overlapping_lotes(predio.id, candidate))
    assert overlaps.keys() == {sibling}
    assert overlaps[sibling] == pytest.approx(sibling.area_m2 / 2, rel=0.01)


def test_sibling_lotes_sharing_a_border_do_not_overlap(
    lotes: LoteRepository, make_predio: MakePredio, make_lote: MakeLote
) -> None:
    predio = make_predio()
    make_lote(predio)
    neighbour = _shape(g.square(lon=g.BASE_LON + HALF, size=HALF))

    assert lotes.find_overlapping_lotes(predio.id, neighbour) == []


def test_lotes_of_another_predio_are_not_siblings(
    lotes: LoteRepository, make_predio: MakePredio, make_lote: MakeLote
) -> None:
    make_lote(make_predio())
    other = make_predio()

    assert lotes.find_overlapping_lotes(other.id, _shape(g.square(size=HALF))) == []


def test_a_lote_does_not_overlap_itself_when_excluded(
    lotes: LoteRepository, make_predio: MakePredio, make_lote: MakeLote
) -> None:
    predio = make_predio()
    lote = make_lote(predio)
    shape = _shape(g.square(size=HALF))

    assert lotes.find_overlapping_lotes(predio.id, shape, exclude_lote_id=lote.id) == []


def test_sum_and_count_ignore_soft_deleted_lotes(
    lotes: LoteRepository, make_predio: MakePredio, make_lote: MakeLote
) -> None:
    predio = make_predio()
    first = make_lote(predio, name="Cuartel 1")
    second = make_lote(
        predio, name="Cuartel 2", geojson=g.square(lon=g.BASE_LON + HALF, size=HALF)
    )
    removed = make_lote(
        predio, name="Cuartel 3", geojson=g.square(lat=g.BASE_LAT + HALF, size=HALF)
    )
    lotes.soft_delete(removed, predio.owner_user_id)

    assert lotes.count_active(predio.id) == 2
    assert lotes.sum_lotes_area(predio.id) == pytest.approx(
        first.area_m2 + second.area_m2
    )
    assert lotes.list_for_predio(predio.id) == [first, second]


def test_sum_of_a_predio_without_lotes_is_zero(
    lotes: LoteRepository, make_predio: MakePredio
) -> None:
    assert lotes.sum_lotes_area(make_predio().id) == 0.0
