"""
Raster → scalar reducers.

New for agkit.io-topography. Two scalar-reducer jobs are wired:

* `watershed_length_slope` (L)         → mean over field polygon
* `watershed_slope_steepness` (LS)     → mean over field polygon

We use a pure-GDAL clip-then-mean: warp the raster to the field's
shapefile with `cropToCutline=True` (we already do this for the
raster-postback path), then read pixels into numpy and reduce with
`np.nanmean`, treating the configured nodata value as NaN.

Reducer functions return `(value, units)`. Units are reported through
to the scalar postback so downstream apps can render them honestly:
* L: pixels of slope length (consumer converts to feet via DEM
  pixel size when needed).
* LS: dimensionless (steepness in percent rise/run × 100).
"""
from __future__ import annotations

from typing import Tuple

import numpy as np
from osgeo import gdal


def _read_band_as_nan(raster_path: str) -> np.ndarray:
    """
    Read band 1 of `raster_path` as a float64 numpy array with the
    raster's nodata value converted to NaN.
    """
    ds = gdal.Open(raster_path)
    if ds is None:
        raise FileNotFoundError(raster_path)
    try:
        band = ds.GetRasterBand(1)
        nodata = band.GetNoDataValue()
        arr = band.ReadAsArray().astype("float64")
        if nodata is not None:
            arr = np.where(arr == nodata, np.nan, arr)
        return arr
    finally:
        ds = None


def mean_inside_field(raster_path: str) -> float:
    """
    Area-mean of valid pixels in `raster_path`. Assumes the raster has
    already been clipped to the field polygon (NaN outside the cutline).
    """
    arr = _read_band_as_nan(raster_path)
    if arr.size == 0 or np.all(np.isnan(arr)):
        return float("nan")
    return float(np.nanmean(arr))


def reduce_length_slope(raster_path: str) -> Tuple[float, str]:
    """L → mean over field; reported in pixels (GRASS r.watershed convention)."""
    return mean_inside_field(raster_path), "pixels"


def reduce_slope_steepness(raster_path: str) -> Tuple[float, str]:
    """LS → mean over field; reported in percent (rise/run × 100)."""
    return mean_inside_field(raster_path), "%"
