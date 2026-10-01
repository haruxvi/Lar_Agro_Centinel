"""Anomaly detection: spatial z-score within a lote, clusters, polygons, severity.

Synthetic arrays only. The lote's statistics come from lote_statistics, the
same function the pipeline uses, so detection is tested on what it receives.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
import shapely
from rasterio.transform import Affine, from_origin
from shapely.geometry import box

from app.modules.analysis.anomaly import (
    DetectedAnomaly,
    SpatialZScore,
    classify_severity,
    pixel_area_m2,
)
from app.modules.analysis.ndvi import LoteNdviStats, NdviResult, lote_statistics
from app.shared.config import Settings
from app.shared.enums import AnomalySeverity, LoteStatsExclusion, LoteType

LON0, LAT0 = -71.3650, -34.6300
RES_X, RES_Y = 0.0001094, 0.00009044  # ~10 m at this latitude
SIZE = 60
TRANSFORM: Affine = from_origin(LON0, LAT0, RES_X, RES_Y)
HEALTHY, STRESSED = 0.70, 0.20


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "secret_key": "a-real-secret-key-with-at-least-32-chars",
        "totp_secret_encryption_key": "a-different-real-key-of-32-chars-min",
        "database_url": "postgresql+psycopg://lar_app:pw@localhost:5432/db",
        "database_migration_url": "postgresql+psycopg://lar_owner:pw@localhost:5432/db",
        "redis_url": "redis://localhost:6379/0",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _pixels(col0: int, row0: int, col1: int, row1: int) -> Any:
    """Return the lon/lat box covering whole pixels [col0, col1) x [row0, row1)."""
    west, north = TRANSFORM * (col0, row0)
    east, south = TRANSFORM * (col1, row1)
    return box(west, south, east, north)


LOTE = _pixels(10, 10, 30, 30)  # 20 x 20 = 400 pixels


def _field(value: float = HEALTHY) -> Any:
    ndvi = np.full((SIZE, SIZE), np.nan, dtype=np.float32)
    ndvi[10:30, 10:30] = value
    return ndvi


def _detect(
    ndvi: Any, lote: Any = LOTE, settings: Settings | None = None
) -> list[DetectedAnomaly]:
    settings = settings or _settings()
    result = NdviResult(
        ndvi.astype(np.float32),
        TRANSFORM,
        "EPSG:4326",
        int(np.isfinite(ndvi).sum()),
        ndvi.size,
        {},
    )
    stats = lote_statistics(result, lote, LoteType.CUARTEL, settings)
    return SpatialZScore().detect(result, lote, stats, settings)


# --- detection ------------------------------------------------------------------------


def test_a_uniform_lote_has_no_anomalies() -> None:
    assert _detect(_field()) == []


def test_mild_variation_is_not_an_anomaly() -> None:
    ndvi = _field()
    checker = (np.indices((20, 20)).sum(axis=0) % 2) * 0.02 - 0.01
    ndvi[10:30, 10:30] += checker.astype(np.float32)  # z of +-1 everywhere
    assert _detect(ndvi) == []


def test_a_clearly_low_zone_is_found_where_it_is() -> None:
    ndvi = _field()
    ndvi[12:18, 12:18] = STRESSED  # 36 pixels

    [anomaly] = _detect(ndvi)
    assert anomaly.pixel_count == 36
    assert anomaly.mean_index_value == pytest.approx(STRESSED)
    assert anomaly.geometry.intersection(_pixels(12, 12, 18, 18)).area == pytest.approx(
        _pixels(12, 12, 18, 18).area, rel=0.05
    )
    assert anomaly.mean_zscore < -2.0


def test_clusters_below_the_minimum_are_discarded() -> None:
    ndvi = _field()
    ndvi[12:15, 12:15] = STRESSED  # 9 pixels, minimum is 10
    assert _detect(ndvi) == []


def test_pixels_touching_diagonally_form_one_zone() -> None:
    ndvi = _field()
    ndvi[12:17, 12:17] = STRESSED  # 25 pixels
    ndvi[17:22, 17:22] = STRESSED  # 25 more, touching only at a corner

    [anomaly] = _detect(ndvi)
    assert anomaly.pixel_count == 50
    assert anomaly.geometry.geom_type == "Polygon"


def test_separate_zones_are_separate_anomalies() -> None:
    ndvi = _field()
    ndvi[12:16, 12:16] = STRESSED
    ndvi[24:28, 24:28] = STRESSED
    assert len(_detect(ndvi)) == 2


def test_the_polygon_lies_within_the_lote() -> None:
    ndvi = _field()
    ndvi[10:18, 10:18] = STRESSED  # against the lote's corner
    [anomaly] = _detect(ndvi)
    assert LOTE.buffer(1e-12).contains(anomaly.geometry)


def test_the_cluster_mean_zscore_matches_a_direct_computation() -> None:
    rng = np.random.default_rng(3)
    ndvi = _field()
    ndvi[12:18, 12:18] = rng.uniform(0.10, 0.25, (6, 6)).astype(np.float32)
    lote_values = ndvi[10:30, 10:30]
    mean, std = float(lote_values.mean()), float(lote_values.std())

    [anomaly] = _detect(ndvi)
    zone = ndvi[12:18, 12:18]
    expected = ((zone - mean) / std)[(zone - mean) / std <= -2.0]
    assert anomaly.mean_zscore == pytest.approx(float(expected.mean()), rel=1e-4)


def test_a_lote_without_valid_statistics_yields_nothing() -> None:
    ndvi = _field()
    ndvi[12:18, 12:18] = STRESSED
    result = NdviResult(ndvi, TRANSFORM, "EPSG:4326", 400, ndvi.size, {})
    excluded = LoteNdviStats(400, 400, LoteStatsExclusion.INSUFFICIENT_PIXELS)
    assert SpatialZScore().detect(result, LOTE, excluded, _settings()) == []


def test_simplification_reduces_vertices_without_leaving_the_lote() -> None:
    ndvi = _field()
    for step in range(10):  # a staircase: many vertices when unsimplified
        ndvi[12 + step, 12 : 13 + step] = STRESSED
    raw = _detect(ndvi, settings=_settings(anomaly_simplify_tolerance_m=0.0))
    simplified = _detect(ndvi, settings=_settings(anomaly_simplify_tolerance_m=15.0))

    raw_vertices = shapely.get_num_coordinates(raw[0].geometry)
    simplified_vertices = shapely.get_num_coordinates(simplified[0].geometry)
    assert simplified_vertices < raw_vertices
    assert LOTE.buffer(1e-12).contains(simplified[0].geometry)


# --- severity -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mean_z", "area_m2", "ratio", "expected"),
    [
        (-3.5, 6_000.0, 0.01, AnomalySeverity.HIGH),  # strong, large in absolute terms
        (-3.5, 2_000.0, 0.20, AnomalySeverity.HIGH),  # strong, large relative to the lote
        (-3.5, 2_000.0, 0.05, AnomalySeverity.MEDIUM),  # strong only
        (-2.3, 6_000.0, 0.01, AnomalySeverity.MEDIUM),  # large only
        (-2.3, 2_000.0, 0.05, AnomalySeverity.LOW),
    ],
)
def test_severity_cuts(
    mean_z: float, area_m2: float, ratio: float, expected: AnomalySeverity
) -> None:
    assert classify_severity(mean_z, area_m2, ratio, _settings()) is expected


def test_high_by_absolute_area_in_a_large_lote() -> None:
    lote = _pixels(0, 0, SIZE, SIZE)  # 3600 pixels, ~36 ha
    ndvi = np.full((SIZE, SIZE), HEALTHY, dtype=np.float32)
    ndvi[5:13, 5:13] = 0.05  # 64 px: ~6400 m2, but under 2% of the lote

    [anomaly] = _detect(ndvi, lote=lote)
    assert anomaly.area_m2 >= 5_000
    assert anomaly.area_ratio_of_lote < 0.10
    assert anomaly.severity is AnomalySeverity.HIGH


def test_high_by_ratio_in_a_small_lote() -> None:
    # The ratio cut is lowered to 4% here: with the default 10% this branch is
    # all but unreachable (see the test below). The logic is what is tested.
    ndvi = _field()
    ndvi[12:16, 12:17] = 0.05  # 20 px: ~2000 m2, 5% of a 400-pixel lote

    [anomaly] = _detect(ndvi, settings=_settings(anomaly_severity_high_area_ratio=0.04))
    assert anomaly.area_m2 < 5_000
    assert anomaly.area_ratio_of_lote >= 0.04
    assert anomaly.mean_zscore <= -3.0
    assert anomaly.severity is AnomalySeverity.HIGH


@pytest.mark.parametrize("share", [0.06, 0.09, 0.12, 0.20])
def test_with_default_cuts_a_strong_zone_never_exceeds_a_tenth_of_its_lote(
    share: float,
) -> None:
    """Document a property of the method, not just of this code.

    A zone covering a share p of its lote inflates the lote's own spread: its
    mean z-score is bounded by |z| <= sqrt((1 - p) / p), whatever the NDVI
    values. Strong (z <= -3) therefore needs p <= 0.10, so "strong AND at
    least 10% of the lote" is only reachable at exactly 10%. With the default
    cuts, HIGH effectively comes from the absolute area alone.
    """
    lote = _pixels(0, 0, 50, 50)  # 2500 pixels
    ndvi = np.full((SIZE, SIZE), np.nan, dtype=np.float32)
    ndvi[0:50, 0:50] = HEALTHY
    zone_rows = round(share * 2500 / 50)
    ndvi[0:zone_rows, 0:50] = 0.0  # the most extreme contrast possible

    # At 20% the same bound gives |z| <= 2: the zone does not even reach the
    # detection threshold, so nothing at all may be found. That is the bound
    # too, seen from the other side.
    for anomaly in _detect(ndvi, lote=lote):
        bound = ((1 - anomaly.area_ratio_of_lote) / anomaly.area_ratio_of_lote) ** 0.5
        assert abs(anomaly.mean_zscore) <= bound + 1e-4
        strong = anomaly.mean_zscore <= -3.0
        assert not (strong and anomaly.area_ratio_of_lote > 0.10)


def test_medium_when_only_one_condition_holds() -> None:
    ndvi = _field()
    ndvi[12:18, 12:18] = STRESSED  # z ~ -3.2, ~3600 m2, 9% of the lote
    [anomaly] = _detect(ndvi)
    assert anomaly.area_m2 < 5_000 and anomaly.area_ratio_of_lote < 0.10
    assert anomaly.severity is AnomalySeverity.MEDIUM


def test_a_pixel_is_about_a_hundred_square_metres() -> None:
    result = NdviResult(_field(), TRANSFORM, "EPSG:4326", 0, 0, {})
    assert pixel_area_m2(result) == pytest.approx(100.0, rel=0.02)
