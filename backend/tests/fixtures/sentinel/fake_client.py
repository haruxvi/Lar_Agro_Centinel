"""A stand-in for SentinelClient that serves synthetic scenes. Never the API.

The rasters it returns are synthetic (see README.md in this folder): bands in
the reflectance range Sentinel Hub delivers in REFLECTANCE units, laid on the
same grid the real client would request (EPSG:4326, ~10 m at the predio's
latitude, bbox grown by two pixels).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.transform import from_origin
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry

from app.modules.analysis.ndvi import BAND_ORDER
from app.modules.analysis.sentinel_client import (
    CloudObservation,
    NoSuitableSceneFoundError,
    SceneSelection,
    SentinelResponse,
    buffered_bbox,
    degrees_for_metres,
)

SCENE_DATE = date(2026, 9, 15)
SCENE_ID = "S2A_MSIL2A_20260915T143731_N0511_R096_T19HCC_20260915T191008"
HEALTHY_RED, HEALTHY_NIR = 0.05, 0.45  # NDVI 0.8
STRESSED_RED, STRESSED_NIR = 0.15, 0.20  # NDVI ~0.14


@dataclass
class Zone:
    """A rectangle (lon/lat) painted onto the synthetic scene."""

    area: BaseGeometry
    red: float | None = None
    nir: float | None = None
    scl: int | None = None


def synthetic_scene(geometry: BaseGeometry, zones: list[Zone] | None = None) -> bytes:
    """Return a 4-band GeoTIFF (B04, B08, SCL, dataMask) over ``geometry``."""
    resolution = degrees_for_metres(10, geometry.centroid.y)
    min_x, min_y, max_x, max_y = buffered_bbox(geometry, resolution)
    width = math.ceil((max_x - min_x) / resolution[0])
    height = math.ceil((max_y - min_y) / resolution[1])
    transform = from_origin(min_x, max_y, resolution[0], resolution[1])

    red = np.full((height, width), HEALTHY_RED, dtype=np.float32)
    nir = np.full((height, width), HEALTHY_NIR, dtype=np.float32)
    scl = np.full((height, width), 4, dtype=np.float32)  # vegetation
    data_mask = np.ones((height, width), dtype=np.float32)

    for zone in zones or []:
        zone_mask = geometry_mask(
            [zone.area.__geo_interface__], (height, width), transform, invert=True
        )
        if zone.red is not None:
            red[zone_mask] = zone.red
        if zone.nir is not None:
            nir[zone_mask] = zone.nir
        if zone.scl is not None:
            scl[zone_mask] = zone.scl

    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": len(BAND_ORDER),
        "dtype": "float32",
        "crs": "EPSG:4326",
        "transform": transform,
    }
    with rasterio.MemoryFile() as memory:
        with memory.open(**profile) as target:
            for index, band in enumerate((red, nir, scl, data_mask), start=1):
                target.write(band, index)
        return bytes(memory.read())


def corner(geometry: BaseGeometry, fraction: float) -> BaseGeometry:
    """Return the south-west ``fraction`` of the geometry's bbox, as a box."""
    min_x, min_y, max_x, max_y = geometry.bounds
    return box(
        min_x,
        min_y,
        min_x + (max_x - min_x) * fraction,
        min_y + (max_y - min_y) * fraction,
    )


@dataclass
class FakeSentinelClient:
    """Implements the SentinelGateway protocol with canned answers."""

    zones: list[Zone] = field(default_factory=list)
    cloudy: bool = False
    fetch_error: Exception | None = None
    selection_pu: float = 0.4
    fetch_pu: float = 3.1
    scene_id: str = SCENE_ID
    calls: list[str] = field(default_factory=list)
    _spent: float = 0.0

    @property
    def processing_units_spent(self) -> float:
        """Return the PU this fake pretends to have spent."""
        return self._spent

    async def select_scene(
        self, geometry: BaseGeometry, date_from: date, date_to: date
    ) -> SceneSelection:
        """Return the 09-15 scene, or raise as a cloudy month would."""
        self.calls.append("select_scene")
        self._spent += self.selection_pu
        evaluated = [
            CloudObservation(date(2026, 9, 20), 0.85 if not self.cloudy else 0.9, 300),
            CloudObservation(SCENE_DATE, 0.12 if not self.cloudy else 0.47, 300),
        ]
        if self.cloudy:
            raise NoSuitableSceneFoundError(evaluated, 0.30)
        return SceneSelection(SCENE_DATE, self.scene_id, 0.12, evaluated)

    async def fetch_bands(
        self, geometry: BaseGeometry, date_from: date, date_to: date, bands: list[str]
    ) -> SentinelResponse:
        """Return a synthetic scene over the predio, or the configured error."""
        self.calls.append("fetch_bands")
        if self.fetch_error is not None:
            raise self.fetch_error
        self._spent += self.fetch_pu
        resolution = degrees_for_metres(10, geometry.centroid.y)
        return SentinelResponse(
            data=synthetic_scene(geometry, self.zones),
            content_type="image/tiff",
            bands=(*bands, "dataMask"),
            bbox=buffered_bbox(geometry, resolution),
            crs="EPSG:4326",
            resolution=resolution,
            processing_units=self.fetch_pu,
        )
