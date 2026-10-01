"""NDVI: masking, scaling guard, clipping, statistics and outputs.

All data is synthetic (see backend/tests/fixtures/sentinel/README.md): arrays
in the right reflectance range, already scaled the way Sentinel Hub delivers
REFLECTANCE units, with SCL classes mixed in. No test touches the API.
"""

from __future__ import annotations

import io
from typing import Any

import numpy as np
import pytest
import rasterio
from PIL import Image
from rasterio.transform import Affine, from_origin
from shapely.geometry import box

from app.modules.analysis.ndvi import (
    BAND_ORDER,
    InsufficientValidPixelsError,
    NdviResult,
    ReflectanceScaleError,
    check_reflectance,
    compute_ndvi,
    lote_statistics,
    process_scene,
    render_preview,
    resample_nearest,
    scl_valid_mask,
    write_geotiff,
)
from app.shared.config import Settings
from app.shared.enums import LoteStatsExclusion, LoteType

# A 40 x 40 grid of ~10 m pixels near Santa Cruz, Colchagua.
LON0, LAT0 = -71.3650, -34.6300
RES_X, RES_Y = 0.0001094, 0.00009044
SIZE = 40
TRANSFORM: Affine = from_origin(LON0, LAT0, RES_X, RES_Y)
VEGETATION_SCL = 4


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


PREDIO = _pixels(5, 5, 35, 35)  # 30 x 30 = 900 pixels


def _bands(
    red: float = 0.05, nir: float = 0.45, scl: int = VEGETATION_SCL
) -> tuple[Any, Any, Any, Any]:
    shape = (SIZE, SIZE)
    return (
        np.full(shape, red, dtype=np.float32),
        np.full(shape, nir, dtype=np.float32),
        np.full(shape, scl, dtype=np.float32),
        np.ones(shape, dtype=np.float32),
    )


def _tiff(red: Any, nir: Any, scl: Any, data_mask: Any) -> bytes:
    """Encode the four bands as fetch_bands returns them: B04, B08, SCL, dataMask."""
    profile = {
        "driver": "GTiff",
        "height": SIZE,
        "width": SIZE,
        "count": len(BAND_ORDER),
        "dtype": "float32",
        "crs": "EPSG:4326",
        "transform": TRANSFORM,
    }
    with rasterio.MemoryFile() as memory:
        with memory.open(**profile) as target:
            for index, band in enumerate((red, nir, scl, data_mask), start=1):
                target.write(band.astype(np.float32), index)
        return bytes(memory.read())


def _result(ndvi: Any) -> NdviResult:
    finite = int(np.count_nonzero(np.isfinite(ndvi)))
    return NdviResult(
        ndvi.astype(np.float32), TRANSFORM, "EPSG:4326", finite, ndvi.size, {}
    )


# --- the formula -----------------------------------------------------------------


def test_ndvi_of_known_values() -> None:
    red = np.array([[0.1, 0.2, 0.3]], dtype=np.float32)
    nir = np.array([[0.5, 0.2, 0.1]], dtype=np.float32)
    ndvi = compute_ndvi(red, nir, np.ones_like(red, dtype=bool))
    np.testing.assert_allclose(ndvi, [[0.4 / 0.6, 0.0, -0.2 / 0.4]], rtol=1e-6)


def test_a_zero_denominator_is_invalid_never_inf() -> None:
    red = np.array([[0.0, 0.3, 0.1]], dtype=np.float32)
    nir = np.array([[0.0, -0.3, 0.5]], dtype=np.float32)
    ndvi = compute_ndvi(red, nir, np.ones_like(red, dtype=bool))
    assert not np.isinf(ndvi).any()
    assert np.isnan(ndvi[0, 0]) and np.isnan(ndvi[0, 1])
    assert np.isfinite(ndvi[0, 2])


def test_masked_pixels_get_no_value() -> None:
    red, nir = np.full((1, 2), 0.1, np.float32), np.full((1, 2), 0.5, np.float32)
    ndvi = compute_ndvi(red, nir, np.array([[True, False]]))
    assert np.isfinite(ndvi[0, 0]) and np.isnan(ndvi[0, 1])


def test_ndvi_stays_within_minus_one_and_one() -> None:
    rng = np.random.default_rng(7)
    red = rng.uniform(0, 1.2, (50, 50)).astype(np.float32)
    nir = rng.uniform(0, 1.2, (50, 50)).astype(np.float32)
    ndvi = compute_ndvi(red, nir, np.ones_like(red, dtype=bool))
    finite = ndvi[np.isfinite(ndvi)]
    assert finite.min() >= -1.0 and finite.max() <= 1.0


# --- SCL ----------------------------------------------------------------------------


def test_the_scl_mask_excludes_exactly_the_unusable_classes() -> None:
    classes = np.arange(12, dtype=np.float32).reshape(1, 12)
    kept = {
        int(c)
        for c, ok in zip(
            classes[0], scl_valid_mask(classes, exclude_water=True)[0], strict=True
        )
        if ok
    }
    assert kept == {2, 4, 5, 7}  # dark area, vegetation, bare soil, unclassified
    with_water = scl_valid_mask(classes, exclude_water=False)
    assert bool(with_water[0, 6]) is True


def test_nearest_resampling_never_invents_a_class() -> None:
    native = np.array([[4, 8], [9, 4]], dtype=np.uint8)  # 20 m
    upsampled = resample_nearest(native, (4, 4))  # 10 m
    assert set(np.unique(upsampled)) <= {4, 8, 9}  # interpolation would give 6, 6.5...
    np.testing.assert_array_equal(upsampled[:2, :2], 4)
    np.testing.assert_array_equal(upsampled[:2, 2:], 8)
    np.testing.assert_array_equal(upsampled[2:, :2], 9)


# --- scaling guard ------------------------------------------------------------------


def test_unscaled_integers_are_refused_not_turned_into_a_plausible_ndvi() -> None:
    # DN values as they come without REFLECTANCE units: 1500-4000. NDVI over
    # them would still land in [-1, 1] and look perfectly normal.
    red, nir, scl, mask = _bands(red=1500.0, nir=4000.0)
    with pytest.raises(ReflectanceScaleError) as caught:
        process_scene(_tiff(red, nir, scl, mask), PREDIO, _settings())
    assert caught.value.band == "B04"


def test_a_few_bright_pixels_are_tolerated() -> None:
    band = np.full((10, 10), 0.3, dtype=np.float32)
    band[0, 0] = 1.9  # 1% exactly: a glint, not unscaled data
    percentiles = check_reflectance(band, np.ones_like(band, dtype=bool), "B08")
    assert percentiles.p50 == pytest.approx(0.3)


def test_percentiles_are_reported_for_each_band() -> None:
    red, nir, scl, mask = _bands(red=0.05, nir=0.45)
    result = process_scene(_tiff(red, nir, scl, mask), PREDIO, _settings())
    assert result.percentiles["B04"].p50 == pytest.approx(0.05)
    assert result.percentiles["B08"].p99 == pytest.approx(0.45)


# --- the scene ----------------------------------------------------------------------


def test_the_scene_is_clipped_to_the_exact_predio() -> None:
    red, nir, scl, mask = _bands()
    result = process_scene(_tiff(red, nir, scl, mask), PREDIO, _settings())

    assert result.total_pixels == 900
    assert result.valid_pixels == 900
    assert np.isnan(result.ndvi[0, 0])  # outside the predio, inside the bbox
    assert result.ndvi[20, 20] == pytest.approx(0.4 / 0.5)


def test_clouds_are_masked_before_computing() -> None:
    red, nir, scl, mask = _bands()
    scl[5:10, 5:35] = 9  # a cloud bank over 150 of the 900 pixels
    result = process_scene(_tiff(red, nir, scl, mask), PREDIO, _settings())
    assert result.valid_pixels == 750
    assert np.isnan(result.ndvi[7, 20])


def test_too_few_usable_pixels_mean_no_suitable_scene() -> None:
    red, nir, scl, mask = _bands()
    scl[5:25, 5:35] = 8  # 600 of 900 cloudy: 33% usable, 60% required
    with pytest.raises(InsufficientValidPixelsError) as caught:
        process_scene(_tiff(red, nir, scl, mask), PREDIO, _settings())
    assert caught.value.valid_ratio == pytest.approx(300 / 900)


def test_the_geotiff_keeps_crs_transform_and_nodata() -> None:
    red, nir, scl, mask = _bands()
    result = process_scene(_tiff(red, nir, scl, mask), PREDIO, _settings())
    with rasterio.MemoryFile(write_geotiff(result)) as memory, memory.open() as source:
        assert source.crs.to_string() == "EPSG:4326"
        assert source.transform == TRANSFORM
        assert source.dtypes == ("float32",)
        assert np.isnan(source.nodata)
        np.testing.assert_array_equal(np.isnan(source.read(1)), np.isnan(result.ndvi))


def test_the_preview_is_a_transparent_where_empty_red_to_green_png() -> None:
    ndvi = np.array([[np.nan, 0.1, 0.9]], dtype=np.float32)
    image = Image.open(io.BytesIO(render_preview(ndvi)))
    assert image.format == "PNG" and image.mode == "RGBA"
    empty, low, high = (image.getpixel((x, 0)) for x in range(3))
    assert empty[3] == 0
    assert low[0] > low[1]  # red dominates at low NDVI
    assert high[1] > high[0]  # green dominates at high NDVI


# --- per-lote statistics ---------------------------------------------------------------


def test_lote_statistics_match_numpy_on_the_same_pixels() -> None:
    rng = np.random.default_rng(42)
    ndvi = rng.uniform(0.3, 0.9, (SIZE, SIZE)).astype(np.float32)
    lote = _pixels(10, 10, 20, 20)  # 100 pixels
    stats = lote_statistics(_result(ndvi), lote, LoteType.CUARTEL, _settings())

    expected = ndvi[10:20, 10:20]
    assert (stats.valid_pixels, stats.total_pixels, stats.excluded_reason) == (
        100,
        100,
        None,
    )
    assert stats.mean == pytest.approx(float(expected.mean()), rel=1e-6)
    assert stats.median == pytest.approx(float(np.median(expected)), rel=1e-6)
    assert stats.std_dev == pytest.approx(float(expected.std()), rel=1e-5)
    assert stats.min_value == pytest.approx(float(expected.min()))
    assert stats.max_value == pytest.approx(float(expected.max()))
    assert stats.percentile_10 == pytest.approx(
        float(np.percentile(expected, 10)), rel=1e-6
    )
    assert stats.percentile_90 == pytest.approx(
        float(np.percentile(expected, 90)), rel=1e-6
    )


def test_a_greenhouse_is_counted_but_never_measured() -> None:
    ndvi = np.full((SIZE, SIZE), 0.8, dtype=np.float32)
    stats = lote_statistics(
        _result(ndvi), _pixels(10, 10, 20, 20), LoteType.INVERNADERO, _settings()
    )
    assert stats.excluded_reason is LoteStatsExclusion.NOT_APPLICABLE_GREENHOUSE
    assert (stats.valid_pixels, stats.total_pixels) == (100, 100)
    assert stats.mean is None and stats.percentile_90 is None


def test_a_lote_with_fewer_than_20_valid_pixels_has_no_values() -> None:
    ndvi = np.full((SIZE, SIZE), 0.7, dtype=np.float32)
    stats = lote_statistics(
        _result(ndvi), _pixels(10, 10, 14, 14), LoteType.CUARTEL, _settings()
    )
    assert stats.valid_pixels == 16
    assert stats.excluded_reason is LoteStatsExclusion.INSUFFICIENT_PIXELS
    assert stats.mean is None


def test_enough_pixels_but_under_half_the_lote_has_no_values() -> None:
    # 150-pixel lote with 60 valid: well over 20 pixels, but only 40% of it.
    ndvi = np.full((SIZE, SIZE), np.nan, dtype=np.float32)
    ndvi[10:16, 10:20] = 0.7
    stats = lote_statistics(
        _result(ndvi), _pixels(10, 10, 20, 25), LoteType.CUARTEL, _settings()
    )
    assert (stats.valid_pixels, stats.total_pixels) == (60, 150)
    assert stats.excluded_reason is LoteStatsExclusion.INSUFFICIENT_PIXELS


def test_the_inner_buffer_drops_border_pixels_and_is_off_by_default() -> None:
    ndvi = np.full((SIZE, SIZE), 0.7, dtype=np.float32)
    lote = _pixels(10, 10, 30, 30)  # 400 pixels
    default = lote_statistics(_result(ndvi), lote, LoteType.CUARTEL, _settings())
    explicit_zero = lote_statistics(
        _result(ndvi), lote, LoteType.CUARTEL, _settings(analysis_lote_inner_buffer_m=0.0)
    )
    eroded = lote_statistics(
        _result(ndvi),
        lote,
        LoteType.CUARTEL,
        _settings(analysis_lote_inner_buffer_m=10.0),
    )

    assert default == explicit_zero
    assert default.total_pixels == 400
    assert eroded.total_pixels < default.total_pixels
    assert eroded.total_pixels >= 18 * 18  # about one pixel per side
