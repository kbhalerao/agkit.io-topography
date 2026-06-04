"""
Projection-quality regression tests for the two fixes:

  Fix #1 — slope de-striping. `slope_public_10m` reprojects 4326->Albers,
  runs gdaldem slope, and warps back. Doing both warps with nearest-
  neighbour (and letting the back-warp coarsen the grid) injects periodic
  stripes that the slope derivative amplifies on flat ground. The fix uses
  cubicspline resampling and pins the back-warp to the source DEM's grid.

  Fix #2 — RUSLE L/LS. r.watershed computes slope_steepness (LS) and
  length_slope (L) using the region resolution as the metric cell size. In
  EPSG:4326 that resolution is in DEGREES, so LS saturates at r.watershed's
  internal cap and L collapses to ~0. The fix runs the watershed pipeline in
  a projected metric CRS (Albers 5070).

Self-contained: a small synthetic DEM (gentle dome + valley + fine grid-scale
texture, in EPSG:4326 at a mid-CONUS latitude so cells are anisotropic) is
generated on the fly, so these run in the Lambda image / CI without the
external USGS test tile. Each class self-skips when its deps are absent.
"""
import os
import shutil
import unittest

import numpy as np

try:
    from osgeo import gdal, osr
    _GDAL = True
except Exception:
    _GDAL = False

try:
    from app.geoworker import LambdaGISProcessor
    _GEO = True
except Exception:
    _GEO = False

try:
    from app.grass_handler import get_watershed_maps
    _GRASS = True
except Exception:
    _GRASS = False

try:
    from app.grass_handler import get_mfd_flowlines_raw, _FLOW_EPSG, _FLOW_RES_M
    _FLOW = True
except Exception:
    _FLOW = False

_DEM = "tests/_synth_dem_4326.tif"
_FLOW_DEM = "tests/_synth_flow_dem_4326.tif"


def _ensure_dem(path=_DEM):
    """Write a small synthetic DEM in EPSG:4326 once. Gentle relief (~18 m)
    with fine grid-scale texture so nearest-neighbour resampling visibly
    aliases; mid-CONUS latitude so 4326 cells are non-square."""
    if os.path.exists(path):
        return path
    nx, ny = 150, 150
    lon0, lat0 = -90.0, 41.0
    px = 9.2593e-5  # ~10 m at the equator; ~7.8 m E-W ground at lat 41
    yy, xx = np.mgrid[0:ny, 0:nx].astype(float)
    cx = cy = 75.0
    dome = 7.0 * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / 1400.0)
    tilt = 0.05 * yy + 0.02 * xx           # gentle regional slope
    valley = -4.0 * np.exp(-((xx - cx) ** 2) / 240.0)   # N-S draw
    texture = 0.18 * np.sin(xx * 2.3) * np.sin(yy * 2.7)  # near-Nyquist
    elev = (300.0 + tilt + dome + valley + texture).astype(np.float32)

    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, nx, ny, 1, gdal.GDT_Float32)
    ds.SetGeoTransform([lon0, px, 0, lat0, 0, -px])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(0)
    band.WriteArray(elev)
    ds.FlushCache()
    ds = None
    return path


def _ensure_flow_dem(path=_FLOW_DEM):
    """Write a synthetic DEM that drains predominantly due EAST, in EPSG:4326
    at a mid-CONUS latitude. East is the worst case for the 4326 anisotropy
    bug: an E-W cell spans ~7.8 m of ground at lat 41 but r.watershed treats
    it as one unit, so a horizontal flow path's segment lengths come out at
    ~7.8 m instead of the true 10 m. Reprojecting to a metric CRS makes every
    orthogonal step a true 10 m. A faint southward tilt + tiny noise keep the
    D8 routing deterministic without diluting the dominant E-W steps."""
    if os.path.exists(path):
        return path
    nx, ny = 180, 120
    lon0, lat0 = -90.0, 41.0
    px = 9.2593e-5  # ~10 m N-S; ~7.8 m E-W ground at lat 41
    yy, xx = np.mgrid[0:ny, 0:nx].astype(float)
    # Strong eastward fall, faint southward fall, low-amplitude texture so
    # r.fill.dir/r.watershed have no ties but flow stays dominantly E.
    elev = (350.0 - 0.80 * xx - 0.05 * yy
            + 0.05 * np.sin(xx * 1.7) * np.sin(yy * 1.9)).astype(np.float32)

    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, nx, ny, 1, gdal.GDT_Float32)
    ds.SetGeoTransform([lon0, px, 0, lat0, 0, -px])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(0)
    band.WriteArray(elev)
    ds.FlushCache()
    ds = None
    return path


def tearDownModule():
    for p in (_DEM, _DEM + ".cmp.tif", _FLOW_DEM):
        try:
            os.remove(p)
        except OSError:
            pass


def _stripe(arr):
    """Directional 'roughness': mean abs step between adjacent column-means
    (vertical stripes) plus row-means (horizontal stripes). A grid-striped
    field scores high; a clean field scores low."""
    a = np.where(np.isfinite(arr), arr, np.nan)
    v = np.nanmean(np.abs(np.diff(np.nanmean(a, axis=0))))
    h = np.nanmean(np.abs(np.diff(np.nanmean(a, axis=1))))
    return float(v + h)


def _read_on_grid(path, ref):
    """Read `path` masked to physical slope %, resampled onto `ref`'s grid so
    two variants compare cell-for-cell."""
    rds = gdal.Open(ref)
    gt = rds.GetGeoTransform()
    nx, ny = rds.RasterXSize, rds.RasterYSize
    bounds = [gt[0], gt[3] + ny * gt[5], gt[0] + nx * gt[1], gt[3]]
    rds = None
    tmp = path + ".cmp.tif"
    gdal.Warp(tmp, path, dstSRS="EPSG:4326", outputBounds=bounds,
              width=nx, height=ny, resampleAlg="bilinear", format="GTiff")
    ds = gdal.Open(tmp)
    a = ds.GetRasterBand(1).ReadAsArray().astype(float)
    nd = ds.GetRasterBand(1).GetNoDataValue()
    ds = None
    os.remove(tmp)
    if nd is not None:
        a = np.where(a == nd, np.nan, a)
    return np.where((a < 0) | (a > 200), np.nan, a)


def _interior(a, frac=0.18):
    """Drop a `frac` border on every side. Projecting a full-frame DEM fills
    the rotated CRS's corners with nodata; cubicspline rings across that
    300m->0 cliff. Production never sees it (the field+buffer margin absorbs
    the ringing and the result is re-clipped to the field), so the fair
    comparison is on the interior — mirroring that field clip."""
    r, c = a.shape
    dr, dc = int(r * frac), int(c * frac)
    return a[dr:r - dr, dc:c - dc]


def _pixel_size(path):
    ds = gdal.Open(path)
    gt = ds.GetGeoTransform()
    ds = None
    return abs(gt[1]), abs(gt[5])


@unittest.skipUnless(_GEO and _GDAL, "osgeo/geoworker unavailable")
class SlopeDestripeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dem = _ensure_dem()

    def setUp(self):
        self.gis = LambdaGISProcessor()
        self._tmp = []

    def tearDown(self):
        for p in self._tmp:
            try:
                os.remove(p)
            except OSError:
                pass

    def _new_path_slope(self):
        proj = self.gis.gdal_translate_elev(self.dem)
        slp = self.gis.slope(proj, xyunit="m", zunit="m")
        geo = self.gis.gdal_warp_to_geo(slp, ref_grid=self.dem)
        self._tmp += [proj, slp, geo]
        return geo

    def _nearest_baseline_slope(self):
        """The pre-fix behaviour: both warps nearest, back-warp un-pinned."""
        alb = self.dem + ".nbase_alb.tif"
        gdal.Warp(alb, self.dem, dstSRS="EPSG:5070", resampleAlg="near",
                  srcNodata=0, dstNodata=0, format="GTiff")
        slp = self.gis.slope(alb, xyunit="m", zunit="m")
        geo = self.dem + ".nbase_geo.tif"
        gdal.Warp(geo, slp, dstSRS="EPSG:4326", resampleAlg="near",
                  srcNodata=0, dstNodata=0, format="GTiff")
        self._tmp += [alb, slp, geo]
        return geo

    def test_back_warp_preserves_dem_grid(self):
        """The de-striped back-warp must land on the source DEM's grid
        (not a coarsened one) when handed a ref_grid."""
        geo = self._new_path_slope()
        gx, gy = _pixel_size(geo)
        dx, dy = _pixel_size(self.dem)
        self.assertAlmostEqual(gx, dx, delta=dx * 0.01,
                               msg=f"x pixel {gx} != DEM {dx}")
        self.assertAlmostEqual(gy, dy, delta=dy * 0.01,
                               msg=f"y pixel {gy} != DEM {dy}")

    def test_cubicspline_reduces_striping_vs_nearest(self):
        """The cubicspline path must measurably de-stripe relative to the
        old nearest-neighbour path on the same DEM."""
        new_stripe = _stripe(_interior(_read_on_grid(self._new_path_slope(), self.dem)))
        old_stripe = _stripe(_interior(_read_on_grid(self._nearest_baseline_slope(), self.dem)))
        self.assertLess(
            new_stripe, old_stripe * 0.85,
            msg=f"cubicspline stripe {new_stripe:.4f} not <85% of "
                f"nearest {old_stripe:.4f}",
        )


@unittest.skipUnless(_GRASS and _GDAL, "GRASS unavailable")
class WatershedProjectedTests(unittest.TestCase):
    OUTDIR = "tests/out_proj_quality"

    @classmethod
    def setUpClass(cls):
        os.makedirs(cls.OUTDIR, exist_ok=True)
        cls.dem = _ensure_dem()
        cls.result = get_watershed_maps(cls.dem, cls.OUTDIR)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.OUTDIR, ignore_errors=True)

    def test_outputs_are_in_projected_crs(self):
        """L/LS rasters must be computed in a projected (metric) CRS, not
        geographic 4326 — otherwise the RUSLE factors are degree-scaled."""
        ds = gdal.Open(self.result["length_slope"])
        srs = osr.SpatialReference(wkt=ds.GetProjection())
        ds = None
        self.assertTrue(srs.IsProjected(),
                        "length_slope raster is not in a projected CRS")

    def test_LS_not_saturated_at_cap(self):
        """In 4326, r.watershed's LS pegs a large fraction of cells at its
        ~16.3 cap. In a metric CRS the LS field is physical — almost nothing
        should sit at the cap on this gentle DEM."""
        ds = gdal.Open(self.result["slope_steepness"])
        a = ds.GetRasterBand(1).ReadAsArray().astype(float)
        nd = ds.GetRasterBand(1).GetNoDataValue()
        ds = None
        if nd is not None:
            a = a[a != nd]
        a = a[np.isfinite(a)]
        frac_capped = float(np.mean(a >= 16.0))
        self.assertLess(
            frac_capped, 0.02,
            msg=f"{frac_capped*100:.1f}% of LS cells pegged at the cap "
                "(degree-scaled 4326 symptom)",
        )


@unittest.skipUnless(_FLOW and _GDAL, "GRASS flowlines unavailable")
class FlowlinesProjectedTests(unittest.TestCase):
    """mfd_flowlines must run its D8/MFD routing in a projected metric CRS.

    In native 4326 the cells are anisotropic, so (a) r.watershed's flow
    direction is biased toward the compressed axis and (b) path lengths use
    one averaged degree->metre cell size. Running the pipeline in EPSG:5070
    on a 10 m square grid fixes both. These tests assert the projected DEM is
    produced and that profile step lengths match the true 10 m cell pitch
    (which the buggy 4326 path reports as ~7.8 m for an E-W flow line).
    """
    OUTDIR = "tests/out_flow_quality"
    OPTS = {"min_line_length_ft": 60, "max_lines": 4,
            "min_flow_accumulation_cells": 30, "draw_threshold_cells": 200}

    @classmethod
    def setUpClass(cls):
        os.makedirs(cls.OUTDIR, exist_ok=True)
        cls.dem = _ensure_flow_dem()
        cls.raw = get_mfd_flowlines_raw(cls.dem, cls.OUTDIR, options=cls.OPTS)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.OUTDIR, ignore_errors=True)

    def test_flow_dem_is_projected(self):
        """The pipeline must reproject the DEM into the metric flow CRS
        before routing — proven by the projected DEM landing in outdir with
        a projected, square-metre grid."""
        proj = os.path.join(self.OUTDIR, f"flow_dem_epsg{_FLOW_EPSG}.tif")
        self.assertTrue(os.path.exists(proj),
                        "flowlines did not write the projected flow DEM")
        ds = gdal.Open(proj)
        srs = osr.SpatialReference(wkt=ds.GetProjection())
        gt = ds.GetGeoTransform()
        ds = None
        self.assertTrue(srs.IsProjected(), "flow DEM is not in a projected CRS")
        self.assertAlmostEqual(abs(gt[1]), _FLOW_RES_M, delta=0.01)
        self.assertAlmostEqual(abs(gt[5]), _FLOW_RES_M, delta=0.01)

    def test_profile_steps_sit_on_projected_square_grid(self):
        """Consecutive profile vertices are one cell apart, so on a true
        square metric grid every step is the orthogonal pitch (~10 m) or the
        diagonal (~14.14 m) — nothing in between. In the 4326 pipeline the
        anisotropic cells make E-W steps ~7.8 m and diagonals ~12.9 m, which
        fall off that grid. Assert the overwhelming majority of steps land on
        the {10, 10·√2} lattice."""
        root2 = 2 ** 0.5
        steps = []
        for rec in self.raw:
            prof = rec.get("profile_vertices_m") or []
            cum = [p[0] for p in prof]
            steps += [b - a for a, b in zip(cum, cum[1:]) if b - a > 1e-6]
        self.assertGreaterEqual(
            len(steps), 10,
            "synthetic flow DEM produced too few profile steps to assess",
        )
        on_grid = [
            d for d in steps
            if min(abs(d / _FLOW_RES_M - 1.0),
                   abs(d / _FLOW_RES_M - root2)) < 0.03
        ]
        frac = len(on_grid) / len(steps)
        self.assertGreaterEqual(
            frac, 0.9,
            msg=f"only {frac*100:.0f}% of profile steps sit on the projected "
                f"{_FLOW_RES_M:.0f} m square grid (4326 anisotropy puts E-W "
                "steps at ~7.8 m); median step "
                f"{float(np.median(steps)):.2f} m",
        )


if __name__ == "__main__":
    unittest.main()
