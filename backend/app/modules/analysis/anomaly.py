"""Anomaly detection: zones significantly worse than the rest of their lote.

Method: spatial z-score within each lote,

    zscore = (pixel_value - lote_mean) / lote_std

and pixels under ``anomaly_zscore_threshold`` are grouped into zones.

Why spatial and not temporal: comparing a lote with its own history is
better, but needs history that does not exist yet; the spatial z-score works
from the first analysis. Methods share the :class:`AnomalyMethod` shape so a
temporal one can be added later without rewriting this module.

Why within a lote and never across lotes: varieties, ages and management give
different baseline NDVI, so only pixels of the same lote are comparable.

STATISTICAL CAVEAT (KL-006): the z-score assumes a roughly normal, unimodal
distribution. A lote with two clearly different zones (a young part and a
mature one) is bimodal, and this method will flag the whole lower zone as
anomalous. That is not a calculation error: it is the method applied outside
its assumption. This is why every anomaly is a hypothesis for an agronomist
to review, never a diagnosis.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, Protocol

import numpy as np
from rasterio.features import shapes

# scipy ships no type stubs; scipy-stubs would be a new dependency to approve.
from scipy import ndimage  # type: ignore[import-untyped]
from shapely.geometry import MultiPolygon, Point, Polygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from app.modules.analysis.ndvi import (
    BoolArray,
    FloatArray,
    LoteNdviStats,
    NdviResult,
    erode_metres,
    inside_mask,
)
from app.modules.analysis.sentinel_client import (
    METRES_PER_DEGREE_LAT,
    METRES_PER_DEGREE_LON_AT_EQUATOR,
)
from app.shared.config import Settings
from app.shared.enums import AnomalySeverity

# 8-connectivity: pixels touching only at a corner belong to the same zone.
CONNECTIVITY_8: Final = np.ones((3, 3), dtype=bool)

# Below this spread a lote is uniform: nothing stands out, and dividing by it
# would turn rounding noise into huge z-scores.
MIN_STD: Final = 1e-6

# Fuses polygons that only touch at a corner into one, at a scale (~1 cm) far
# below anything a 10 m pixel can mean.
_CORNER_FUSE_DEGREES: Final = 1e-7


@dataclass(frozen=True)
class DetectedAnomaly:
    """A zone of a lote, ready to persist as an ``anomalies`` row."""

    geometry: Polygon
    centroid: Point
    area_m2: float
    area_ratio_of_lote: float
    severity: AnomalySeverity
    mean_zscore: float
    mean_index_value: float
    pixel_count: int


class AnomalyMethod(Protocol):
    """A way of finding anomalous zones in one lote."""

    def detect(
        self,
        result: NdviResult,
        lote_geometry: BaseGeometry,
        stats: LoteNdviStats,
        settings: Settings,
    ) -> list[DetectedAnomaly]:
        """Return the anomalous zones of the lote."""
        ...


def classify_severity(
    mean_zscore: float, area_m2: float, area_ratio: float, settings: Settings
) -> AnomalySeverity:
    """Grade an anomaly. Provisional cuts, to be calibrated (see ADR-004).

    HIGH when strong AND large; MEDIUM when only one holds; LOW otherwise.
    "Large" is absolute OR relative on purpose: 0.5 ha is 1% of a 50 ha
    paddock but half of a 1 ha block, so neither measure alone scales.
    """
    strong = mean_zscore <= settings.anomaly_severity_high_zscore
    large = (
        area_m2 >= settings.anomaly_severity_high_area_m2
        or area_ratio >= settings.anomaly_severity_high_area_ratio
    )
    if strong and large:
        return AnomalySeverity.HIGH
    if strong or large:
        return AnomalySeverity.MEDIUM
    return AnomalySeverity.LOW


def pixel_area_m2(result: NdviResult) -> float:
    """Return the ground area of one pixel at the raster's latitude."""
    res_x, res_y = result.transform.a, -result.transform.e
    _, latitude = result.transform * (0, result.ndvi.shape[0] / 2)
    width = res_x * METRES_PER_DEGREE_LON_AT_EQUATOR * math.cos(math.radians(latitude))
    return float(width * res_y * METRES_PER_DEGREE_LAT)


def _single_polygon(geometry: BaseGeometry) -> Polygon | None:
    """Return one polygon for a zone; corner-touching parts are fused first."""
    if isinstance(geometry, MultiPolygon):
        geometry = geometry.buffer(_CORNER_FUSE_DEGREES)
    if isinstance(geometry, MultiPolygon):
        # Clipping to a concave lote can still split a zone: keep its body.
        geometry = max(geometry.geoms, key=lambda part: part.area)
    return geometry if isinstance(geometry, Polygon) and not geometry.is_empty else None


class SpatialZScore:
    """Pixels far below their own lote's mean, grouped into zones."""

    def detect(
        self,
        result: NdviResult,
        lote_geometry: BaseGeometry,
        stats: LoteNdviStats,
        settings: Settings,
    ) -> list[DetectedAnomaly]:
        """Return the anomalous zones of one lote, simplified and graded."""
        if (
            stats.excluded_reason is not None
            or stats.mean is None
            or stats.std_dev is None
        ):
            return []  # a lote that was not measured has nothing to compare to
        if stats.std_dev < MIN_STD:
            return []  # uniform lote

        measured = erode_metres(lote_geometry, settings.analysis_lote_inner_buffer_m)
        inside: BoolArray = inside_mask(measured, result.transform, result.ndvi.shape)
        valid = inside & np.isfinite(result.ndvi)
        zscore: FloatArray = np.full(result.ndvi.shape, np.nan, dtype=np.float32)
        zscore[valid] = (result.ndvi[valid] - stats.mean) / stats.std_dev
        below = valid & (zscore <= settings.anomaly_zscore_threshold)

        labels, count = ndimage.label(below, structure=CONNECTIVITY_8)
        if count == 0:
            return []
        index = np.arange(1, count + 1)
        sizes = ndimage.sum_labels(below, labels, index)
        # Per-cluster means in one vectorised pass over the arrays.
        mean_z = ndimage.mean(np.nan_to_num(zscore), labels, index)
        mean_ndvi = ndimage.mean(np.nan_to_num(result.ndvi), labels, index)
        kept = {
            int(label): position
            for position, label in enumerate(index)
            if sizes[position] >= settings.anomaly_min_cluster_pixels
        }
        if not kept:
            return []

        polygons: dict[int, list[BaseGeometry]] = {label: [] for label in kept}
        for geojson, value in shapes(
            labels.astype(np.int32),
            mask=np.isin(labels, list(kept)),
            transform=result.transform,
        ):
            polygons[int(value)].append(shape(geojson))

        pixel_area = pixel_area_m2(result)
        tolerance = settings.anomaly_simplify_tolerance_m / METRES_PER_DEGREE_LAT
        lote_pixels = max(stats.total_pixels, 1)
        anomalies: list[DetectedAnomaly] = []
        for label, position in kept.items():
            zone = unary_union(polygons[label]).simplify(
                tolerance, preserve_topology=True
            )
            polygon = _single_polygon(zone.intersection(lote_geometry))
            if polygon is None:
                continue
            pixel_count = int(sizes[position])
            area = pixel_count * pixel_area
            ratio = min(1.0, pixel_count / lote_pixels)
            zscore_mean = float(mean_z[position])
            anomalies.append(
                DetectedAnomaly(
                    geometry=polygon,
                    centroid=polygon.centroid,
                    area_m2=area,
                    area_ratio_of_lote=ratio,
                    severity=classify_severity(zscore_mean, area, ratio, settings),
                    mean_zscore=zscore_mean,
                    mean_index_value=float(mean_ndvi[position]),
                    pixel_count=pixel_count,
                )
            )
        return sorted(anomalies, key=lambda anomaly: anomaly.mean_zscore)
