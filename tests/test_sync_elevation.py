"""
Tests for the synchronous elevation renderer (`app.sync_elevation`).

Two layers:

* Hermetic — tile naming (pure) + a full render against the local fixture
  tile `tests/USGS_13_n42w092.tif` in IN_TEST mode. Runs inside the Lambda
  image via `python -m unittest discover`; skipped where GDAL is absent
  (the host has no GDAL — see memory `lambda-image-runtime`).

* Live (opt-in) — a real read from the public prd-tnm bucket for an Oregon
  boundary. Skipped unless `LIVE_DEM=1`, so the default suite stays offline.
  Run it in the image to validate the actual `/vsis3/` COG read:

      docker run --rm -e LIVE_DEM=1 agkit-topography:latest \
          python -m unittest tests.test_sync_elevation -v
"""
import os
import unittest

os.environ.setdefault("IN_TEST", "true")

try:
    from osgeo import gdal  # noqa: F401
    from app import sync_elevation
    HAVE_GDAL = True
except Exception:  # ImportError, or gdal present but broken
    sync_elevation = None
    HAVE_GDAL = False

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# GeoTIFF byte-order magic: little-endian ("II*\x00") or big-endian ("MM\x00*").
TIFF_MAGICS = (b"II*\x00", b"MM\x00*")


@unittest.skipUnless(HAVE_GDAL, "GDAL not available (run inside the Lambda image)")
class TileNamingTests(unittest.TestCase):
    def test_nw_corner_name(self):
        # Corvallis, OR sits in the tile whose NW corner is 45 N, 124 W.
        self.assertEqual(sync_elevation._tile_name(45, 124), "n45w124")

    def test_single_tile_bbox(self):
        tiles = sync_elevation.tiles_for_bbox(-123.30, 44.55, -123.29, 44.56)
        self.assertEqual(len(tiles), 1)
        self.assertIn("n45w124", tiles[0])

    def test_bbox_straddling_two_tiles(self):
        # Crosses the 124 W tile edge -> two tiles.
        tiles = sync_elevation.tiles_for_bbox(-124.02, 44.5, -123.98, 44.6)
        self.assertEqual(len(tiles), 2)
        self.assertTrue(any("n45w124" in t for t in tiles))
        self.assertTrue(any("n45w125" in t for t in tiles))

    def test_oversized_boundary_rejected(self):
        # A multi-degree box exceeds MAX_TILES.
        big = {"type": "Polygon", "coordinates": [[
            [-124.0, 42.0], [-120.0, 42.0], [-120.0, 46.0],
            [-124.0, 46.0], [-124.0, 42.0],
        ]]}
        with self.assertRaises(ValueError):
            sync_elevation.render_elevation(big)


@unittest.skipUnless(HAVE_GDAL, "GDAL not available (run inside the Lambda image)")
class RenderFromFixtureTests(unittest.TestCase):
    """IN_TEST mode reads the local fixture tile, so this is fully offline."""

    # A ~1.5 km box inside the n42w092 fixture (41-42 N, 92-91 W).
    BOUNDARY = {"type": "Polygon", "coordinates": [[
        [-91.51, 41.49], [-91.49, 41.49],
        [-91.49, 41.51], [-91.51, 41.51], [-91.51, 41.49],
    ]]}

    @classmethod
    def setUpClass(cls):
        fixture = os.path.join(os.path.dirname(__file__), "USGS_13_n42w092.tif")
        if not os.path.exists(fixture):
            raise unittest.SkipTest(f"fixture tile missing: {fixture}")

    def test_returns_png_bytes(self):
        result = sync_elevation.render_elevation(self.BOUNDARY)
        self.assertTrue(result["png"].startswith(PNG_MAGIC))
        self.assertGreater(result["width"], 0)
        self.assertGreater(result["height"], 0)

    def test_returns_tif_data(self):
        # The GeoTIFF is the actual clipped elevation data, shipped alongside
        # the display PNG. It must be a real, georeferenced single-band raster.
        result = sync_elevation.render_elevation(self.BOUNDARY)
        self.assertTrue(result["tif"].startswith(TIFF_MAGICS))
        gdal.FileFromMemBuffer("/vsimem/_test_clip.tif", result["tif"])
        try:
            ds = gdal.Open("/vsimem/_test_clip.tif")
            self.assertIsNotNone(ds)
            self.assertEqual(ds.RasterCount, 1)
            self.assertEqual(ds.RasterXSize, result["width"])
            self.assertEqual(ds.RasterYSize, result["height"])
            self.assertNotEqual(ds.GetProjection(), "")  # carries georeferencing
            ds = None
        finally:
            gdal.Unlink("/vsimem/_test_clip.tif")

    def test_extent_brackets_boundary(self):
        result = sync_elevation.render_elevation(self.BOUNDARY)
        w, s, e, n = result["extent"]
        # The clipped extent (boundary + buffer, pixel-snapped) must enclose
        # the input boundary.
        self.assertLessEqual(w, -91.51)
        self.assertGreaterEqual(e, -91.49)
        self.assertLessEqual(s, 41.49)
        self.assertGreaterEqual(n, 41.51)


@unittest.skipUnless(HAVE_GDAL and os.environ.get("LIVE_DEM") == "1",
                     "live prd-tnm read (set LIVE_DEM=1 inside the image)")
class RenderLiveOregonTests(unittest.TestCase):
    """Real `/vsis3/` read from prd-tnm — validates the unsigned COG path."""

    def setUp(self):
        # Force the real bucket path even though IN_TEST is set for the suite.
        self._saved = sync_elevation.settings.IN_TEST
        sync_elevation.settings.IN_TEST = False

    def tearDown(self):
        sync_elevation.settings.IN_TEST = self._saved

    def test_corvallis(self):
        boundary = {"type": "Polygon", "coordinates": [[
            [-123.30, 44.55], [-123.29, 44.55],
            [-123.29, 44.56], [-123.30, 44.56], [-123.30, 44.55],
        ]]}
        result = sync_elevation.render_elevation(boundary)
        self.assertTrue(result["png"].startswith(PNG_MAGIC))
        self.assertGreater(len(result["png"]), 100)
        self.assertTrue(result["tif"].startswith(TIFF_MAGICS))
        self.assertGreater(len(result["tif"]), 100)


if __name__ == "__main__":
    unittest.main()
