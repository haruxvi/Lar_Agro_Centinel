"""NDVI from Sentinel-2 L2A: masking, scaling guard, clipping and statistics.

    NDVI = (NIR - RED) / (NIR + RED) = (B08 - B04) / (B08 + B04)

Range [-1, 1]: healthy vegetation 0.6-0.9, bare soil 0.1-0.2, water negative.

Scaling is delegated to Sentinel Hub: bands are requested in REFLECTANCE units
with harmonizeValues, so the service applies the 1/10000 factor and removes
the processing-baseline 04.00 offset (see ADR-004). Ignoring that offset
gives an NDVI that is wrong without ever looking wrong. The factor here is
therefore 1.0, and :func:`check_reflectance` refuses any raster whose values
look unscaled instead of turning them into a plausible-looking NDVI.

Everything works in float32 and on pure arrays, so it is tested with
synthetic data and never needs the API.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Final

import numpy as np
import numpy.typing as npt
import rasterio
from PIL import Image
from pyproj import Transformer
from rasterio.features import geometry_mask
from rasterio.transform import Affine
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

from app.modules.analysis.exceptions import AnalysisError
from app.shared.config import Settings
from app.shared.enums import LoteStatsExclusion, LoteType

FloatArray = npt.NDArray[np.float32]
BoolArray = npt.NDArray[np.bool_]

# Sentinel Hub returns REFLECTANCE already scaled and harmonised: nothing to
# multiply here. Kept explicit so nobody "fixes" it into a second scaling.
REFLECTANCE_SCALE_FACTOR: Final = 1.0

# Reflectance may exceed 1.0 over very bright surfaces, but not by much and
# not in many pixels. More than 1% outside this range means the values were
# not scaled (DN integers) and the raster is refused.
REFLECTANCE_RANGE: Final = (0.0, 1.6)
REFLECTANCE_MAX_OUTSIDE_RATIO: Final = 0.01

# SCL classes never used: no data, saturated/defective, cloud shadow, cloud
# (medium, high), thin cirrus, snow/ice. Water (6) is configurable.
EXCLUDED_SCL_CLASSES: Final = frozenset({0, 1, 3, 8, 9, 10, 11})
SCL_WATER: Final = 6

# Below this, NIR + RED is effectively zero and the ratio is noise.
DENOMINATOR_EPSILON: Final = 1e-6

# Band order of the raster fetched by SentinelClient.fetch_bands.
BAND_ORDER: Final = ("B04", "B08", "SCL", "dataMask")


class ReflectanceScaleError(AnalysisError):
    """Band values are outside the reflectance range: probably unscaled."""

    def __init__(self, band: str, outside_ratio: float) -> None:
        """Record which band failed and how much of it was out of range."""
        super().__init__(
            f"{band}: {outside_ratio:.1%} of valid pixels outside reflectance range"
        )
        self.band = band
        self.outside_ratio = outside_ratio

    def as_dict(self) -> dict[str, object]:
        """Return the failing band and the share out of range."""
        return {
            "error": type(self).__name__,
            "band": self.band,
            "outside_ratio": round(self.outside_ratio, 4),
        }


class InsufficientValidPixelsError(AnalysisError):
    """The scene passed the cloud filter but too little of the predio is usable."""

    def __init__(self, valid_ratio: float, minimum: float) -> None:
        """Record the usable share against the minimum."""
        super().__init__(f"{valid_ratio:.1%} valid pixels, {minimum:.0%} required")
        self.valid_ratio = valid_ratio
        self.minimum = minimum

    def as_dict(self) -> dict[str, object]:
        """Return the usable share and the minimum."""
        return {
            "error": type(self).__name__,
            "valid_pixels_ratio": round(self.valid_ratio, 4),
            "min_valid_pixels_ratio": self.minimum,
        }


@dataclass(frozen=True)
class BandPercentiles:
    """Percentiles 1, 50 and 99 of a band over its valid pixels.

    Logged on every completed analysis: if values ever come back unscaled,
    these numbers show it at once even before the guard fires.
    """

    p1: float
    p50: float
    p99: float


@dataclass(frozen=True)
class NdviResult:
    """NDVI over a predio, clipped to its exact geometry."""

    ndvi: FloatArray
    """NaN where there is no valid value (outside the predio, masked, ...)."""
    transform: Affine
    crs: str
    valid_pixels: int
    total_pixels: int
    percentiles: dict[str, BandPercentiles]

    @property
    def valid_ratio(self) -> float:
        """Return the share of the predio's pixels with a valid NDVI."""
        return self.valid_pixels / self.total_pixels if self.total_pixels else 0.0


@dataclass(frozen=True)
class LoteNdviStats:
    """A lote's NDVI statistics, or why it has none."""

    valid_pixels: int
    total_pixels: int
    excluded_reason: LoteStatsExclusion | None = None
    mean: float | None = None
    median: float | None = None
    std_dev: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    percentile_10: float | None = None
    percentile_90: float | None = None


# --- masking and NDVI -------------------------------------------------------------


def resample_nearest(
    classes: npt.NDArray[np.generic], shape: tuple[int, int]
) -> npt.NDArray[np.generic]:
    """Resample a class raster to ``shape`` by nearest neighbour.

    Used when SCL arrives at its native 20 m while the bands are at 10 m. The
    current Process request already asks Sentinel Hub for NEAREST upsampling,
    so the main path does not call this; it is kept, tested, for SCL served at
    native resolution. Never interpolate a class mask: the average of class 4
    and class 8 is not class 6, it is nonsense.
    """
    rows = (np.arange(shape[0]) * classes.shape[0]) // shape[0]
    cols = (np.arange(shape[1]) * classes.shape[1]) // shape[1]
    return classes[np.ix_(rows, cols)]


def scl_valid_mask(scl: npt.NDArray[np.generic], *, exclude_water: bool) -> BoolArray:
    """Return True where the scene classification allows measuring vegetation."""
    excluded = set(EXCLUDED_SCL_CLASSES)
    if exclude_water:
        excluded.add(SCL_WATER)
    classes = np.rint(np.asarray(scl, dtype=np.float32)).astype(np.int16)
    mask: BoolArray = ~np.isin(classes, sorted(excluded))
    return mask


def check_reflectance(band: FloatArray, valid: BoolArray, name: str) -> BandPercentiles:
    """Refuse a band that looks unscaled; return its percentiles otherwise."""
    values = band[valid & np.isfinite(band)]
    if values.size == 0:
        return BandPercentiles(float("nan"), float("nan"), float("nan"))
    low, high = REFLECTANCE_RANGE
    outside = float(np.count_nonzero((values < low) | (values > high))) / values.size
    if outside > REFLECTANCE_MAX_OUTSIDE_RATIO:
        raise ReflectanceScaleError(name, outside)
    p1, p50, p99 = np.percentile(values, [1, 50, 99])
    return BandPercentiles(float(p1), float(p50), float(p99))


def compute_ndvi(red: FloatArray, nir: FloatArray, valid: BoolArray) -> FloatArray:
    """Return NDVI where ``valid``; NaN elsewhere, never inf or a silent NaN.

    A near-zero denominator marks the pixel invalid instead of dividing.
    """
    red32 = red.astype(np.float32) * REFLECTANCE_SCALE_FACTOR
    nir32 = nir.astype(np.float32) * REFLECTANCE_SCALE_FACTOR
    denominator = nir32 + red32
    usable = valid & np.isfinite(red32) & np.isfinite(nir32)
    usable &= np.abs(denominator) > DENOMINATOR_EPSILON
    ndvi = np.full(red32.shape, np.nan, dtype=np.float32)
    ndvi[usable] = (nir32[usable] - red32[usable]) / denominator[usable]
    return np.clip(ndvi, -1.0, 1.0, out=ndvi, where=usable)


def inside_mask(
    geometry: BaseGeometry, transform: Affine, shape: tuple[int, int]
) -> BoolArray:
    """Return True for pixels whose centre lies inside ``geometry``."""
    mask: BoolArray = geometry_mask(
        [geometry.__geo_interface__], out_shape=shape, transform=transform, invert=True
    )
    return mask


# --- the scene ----------------------------------------------------------------------


def process_scene(
    data: bytes, predio_geometry: BaseGeometry, settings: Settings
) -> NdviResult:
    """Turn the fetched GeoTIFF (B04, B08, SCL, dataMask) into a clipped NDVI.

    Masks with SCL first, checks the scaling, computes NDVI, clips to the
    exact predio, and refuses the scene when too little of it is usable.
    """
    with rasterio.MemoryFile(data) as memory, memory.open() as source:
        if source.count != len(BAND_ORDER):
            raise AnalysisError(f"expected {len(BAND_ORDER)} bands, got {source.count}")
        red, nir, scl, data_mask = (
            source.read(i + 1).astype(np.float32) for i in range(4)
        )
        transform: Affine = source.transform
        crs = source.crs.to_string() if source.crs else "EPSG:4326"

    valid = scl_valid_mask(scl, exclude_water=settings.analysis_exclude_water)
    valid &= data_mask > 0
    percentiles = {
        "B04": check_reflectance(red, valid, "B04"),
        "B08": check_reflectance(nir, valid, "B08"),
    }
    ndvi = compute_ndvi(red, nir, valid)

    inside = inside_mask(predio_geometry, transform, ndvi.shape)
    ndvi[~inside] = np.nan
    total = int(np.count_nonzero(inside))
    measured = int(np.count_nonzero(inside & np.isfinite(ndvi)))
    result = NdviResult(ndvi, transform, crs, measured, total, percentiles)
    if result.valid_ratio < settings.analysis_min_valid_pixels_ratio:
        raise InsufficientValidPixelsError(
            result.valid_ratio, settings.analysis_min_valid_pixels_ratio
        )
    return result


# --- outputs --------------------------------------------------------------------------


def write_geotiff(result: NdviResult) -> bytes:
    """Encode the NDVI as a float32 GeoTIFF with explicit NaN nodata and its CRS."""
    height, width = result.ndvi.shape
    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": "float32",
        "crs": result.crs,
        "transform": result.transform,
        "nodata": float("nan"),
        "compress": "deflate",
    }
    with rasterio.MemoryFile() as memory:
        with memory.open(**profile) as target:
            target.write(result.ndvi, 1)
            target.set_band_description(1, "NDVI")
        return bytes(memory.read())


# Red at stressed or bare (<= 0.2), yellow at moderate (0.45), green at dense
# vegetation (>= 0.8). Linear in between.
_PALETTE_STOPS: Final = (
    (0.2, (215, 48, 39)),
    (0.45, (254, 224, 139)),
    (0.8, (26, 152, 80)),
)


def render_preview(ndvi: FloatArray) -> bytes:
    """Render a red-yellow-green PNG, transparent where there is no value."""
    positions = np.array([stop for stop, _ in _PALETTE_STOPS], dtype=np.float32)
    colours = np.array([colour for _, colour in _PALETTE_STOPS], dtype=np.float32)
    values = np.nan_to_num(ndvi, nan=positions[0])
    rgba = np.zeros((*ndvi.shape, 4), dtype=np.uint8)
    for channel in range(3):
        rgba[..., channel] = np.interp(values, positions, colours[:, channel]).astype(
            np.uint8
        )
    rgba[..., 3] = np.where(np.isfinite(ndvi), 255, 0).astype(np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


# --- per-lote statistics ----------------------------------------------------------------


def erode_metres(geometry: BaseGeometry, metres: float) -> BaseGeometry:
    """Shrink a lon/lat geometry by ``metres``, measured on the ground.

    Buffered in a local azimuthal equidistant projection centred on the
    geometry, so the distance is the same in every direction.
    """
    if metres <= 0:
        return geometry
    centre = geometry.centroid
    local = f"+proj=aeqd +lat_0={centre.y} +lon_0={centre.x} +units=m +datum=WGS84"
    to_local = Transformer.from_crs("EPSG:4326", local, always_xy=True).transform
    to_lonlat = Transformer.from_crs(local, "EPSG:4326", always_xy=True).transform
    eroded = shapely_transform(to_local, geometry).buffer(-metres)
    return shapely_transform(to_lonlat, eroded)


def lote_statistics(
    result: NdviResult,
    lote_geometry: BaseGeometry,
    lote_type: LoteType,
    settings: Settings,
) -> LoteNdviStats:
    """Return a lote's NDVI statistics, or its counts and why it has none.

    Greenhouses are never measured by satellite: Sentinel-2 sees the plastic
    roof, not the crop. The number would not be imprecise, it would be about
    something else.
    """
    geometry = erode_metres(lote_geometry, settings.analysis_lote_inner_buffer_m)
    if geometry.is_empty:
        inside: BoolArray = np.zeros(result.ndvi.shape, dtype=bool)
    else:
        inside = inside_mask(geometry, result.transform, result.ndvi.shape)
    values = result.ndvi[inside]
    values = values[np.isfinite(values)]
    total = int(np.count_nonzero(inside))
    valid = int(values.size)

    if lote_type is LoteType.INVERNADERO:
        return LoteNdviStats(valid, total, LoteStatsExclusion.NOT_APPLICABLE_GREENHOUSE)
    enough = valid >= settings.analysis_lote_min_valid_pixels
    representative = total > 0 and valid / total >= settings.analysis_lote_min_valid_ratio
    if not (enough and representative):
        return LoteNdviStats(valid, total, LoteStatsExclusion.INSUFFICIENT_PIXELS)

    p10, median, p90 = np.percentile(values, [10, 50, 90])
    return LoteNdviStats(
        valid_pixels=valid,
        total_pixels=total,
        mean=float(np.mean(values)),
        median=float(median),
        std_dev=float(np.std(values)),
        min_value=float(np.min(values)),
        max_value=float(np.max(values)),
        percentile_10=float(p10),
        percentile_90=float(p90),
    )
