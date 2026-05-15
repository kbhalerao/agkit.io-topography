"""
Verify the LambdaGISProcessor exposes every named function the Django
side may emit, including the new L/LS raster + scalar entries.

This test imports `app.geoworker`, which transitively imports osgeo +
numpy. If those aren't available locally, the test is skipped — CI
inside the Lambda image will still exercise it.
"""
import unittest


try:
    from app.geoworker import LambdaGISProcessor
    _GEOWORKER_AVAILABLE = True
except Exception:
    _GEOWORKER_AVAILABLE = False


@unittest.skipUnless(_GEOWORKER_AVAILABLE, "osgeo/numpy not installed locally")
class GeoWorkerNamedFunctionsTests(unittest.TestCase):
    EXPECTED = (
        # Carried over from USGS2021
        "elev_public_10m",
        "slope_public_10m",
        "watershed_drainage",
        "watershed_streambeds",
        "watershed_spi",
        "watershed_tci",
        "rasterize_and_colorize",
        # NEW — L / LS rasters
        "watershed_length_slope_raster",
        "watershed_slope_steepness_raster",
        # NEW — L / LS scalars
        "watershed_length_slope",
        "watershed_slope_steepness",
        # NEW — contour vector lines
        "contour_lines",
        # NEW — MFD flow lines (RUSLE2 slope shooting)
        "mfd_flowlines",
    )

    def test_all_named_functions_callable(self):
        gis = LambdaGISProcessor()
        for name in self.EXPECTED:
            with self.subTest(function=name):
                fn = getattr(gis, name, None)
                self.assertTrue(callable(fn), f"missing or non-callable: {name}")


if __name__ == "__main__":
    unittest.main()
