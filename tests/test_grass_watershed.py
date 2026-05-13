"""
GRASS r.watershed test — exercises L and LS exports.

Skipped automatically when grass isn't on the path; intended to run
inside the Lambda image where GRASS is installed.
"""
import os
import shutil
import unittest


try:
    from app.grass_handler import get_watershed_maps, RASTER_OUTPUTS
    _GRASS_AVAILABLE = True
except Exception:
    _GRASS_AVAILABLE = False


@unittest.skipUnless(
    _GRASS_AVAILABLE and os.path.exists("tests/elevation_public_10m.tif"),
    "GRASS or test elevation tif unavailable",
)
class WaterShedTests(unittest.TestCase):
    OUTDIR = "tests/out"

    def setUp(self):
        os.makedirs(self.OUTDIR, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.OUTDIR, ignore_errors=True)

    def test_grass_watershed_returns_all_layers_incl_L_LS(self):
        result = get_watershed_maps("tests/elevation_public_10m.tif", self.OUTDIR)
        for name in RASTER_OUTPUTS:
            with self.subTest(layer=name):
                self.assertIn(name, result)
                self.assertTrue(
                    os.path.exists(result[name]),
                    f"{name} raster not on disk: {result[name]}",
                )


class WaterShedConfigTests(unittest.TestCase):
    """Lightweight sanity checks that don't require GRASS on PATH."""

    def test_raster_outputs_includes_L_and_LS(self):
        if not _GRASS_AVAILABLE:
            self.skipTest("grass_handler import failed")
        self.assertIn("length_slope", RASTER_OUTPUTS)
        self.assertIn("slope_steepness", RASTER_OUTPUTS)


if __name__ == "__main__":
    unittest.main()
