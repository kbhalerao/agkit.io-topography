"""
GDAL-backed geometry tests, carried over from USGS2021.

Skipped automatically when osgeo isn't installed locally. The image's
test stage runs these against the canonical DEM tiles under tests/.
"""
import json
import os
import unittest
from tempfile import NamedTemporaryFile


try:
    from app.geoworker import LambdaGISProcessor, process_payload  # noqa: F401
    _IMPORTS_OK = True
except Exception:
    _IMPORTS_OK = False


@unittest.skipUnless(_IMPORTS_OK, "osgeo/numpy not installed locally")
class LambdaGeoWorkerTests(unittest.TestCase):
    def setUp(self):
        # Minimal payload — small box in central IL, no DEM tiles required
        # for extent/geometry math.
        self.payload = [{
            "metadata": {
                "function_name": "elev_public_10m",
                "field_boundary": {
                    "type": "FeatureCollection",
                    "features": [{
                        "type": "Feature",
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[
                                [-88.001, 40.001],
                                [-88.001, 40.002],
                                [-87.999, 40.002],
                                [-87.999, 40.001],
                                [-88.001, 40.001],
                            ]],
                        },
                    }],
                },
                "input_data": None,
                "field_id": 25127,
                "site_prefix": "local-test",
            },
            "post": {
                "parameter": "elevation_public_10m",
                "domain": "http://localhost:8000",
                "output": [],
            },
        }]

        with NamedTemporaryFile(suffix=".json", delete=False) as job_file:
            job_file.write(json.dumps(self.payload).encode("utf-8"))
            self.job_file_name = job_file.name

    def tearDown(self):
        try:
            os.unlink(self.job_file_name)
        except OSError:
            pass

    def test_round_higher(self):
        gis = LambdaGISProcessor()
        self.assertEqual(gis._round_higher(1.22), 2)
        self.assertEqual(gis._round_higher(2.22), 3)
        self.assertEqual(gis._round_higher(-1.22), 2)

    def test_round_lower(self):
        gis = LambdaGISProcessor()
        self.assertEqual(gis._round_lower(1.22), 1)
        self.assertEqual(gis._round_lower(2.22), 2)
        self.assertEqual(gis._round_lower(-1.22), 1)

    def test_field_extent(self):
        gis = LambdaGISProcessor()
        extent = gis.get_field_extent(self.payload[0]["metadata"]["field_boundary"])
        self.assertAlmostEqual(extent[0], -88.001)
        self.assertAlmostEqual(extent[1], 40.001)
        self.assertAlmostEqual(extent[2], -87.999)
        self.assertAlmostEqual(extent[3], 40.002)


@unittest.skipUnless(
    _IMPORTS_OK and os.path.exists("tests/USGS_13_n40w088.tif"),
    "DEM tile fixtures absent — see README for download",
)
class LambdaWithDemTilesTests(unittest.TestCase):
    """Tests that need the actual USGS DEM tiles in tests/."""

    def test_combine_rasters(self):
        rasters = ["tests/USGS_13_n40w088.tif", "tests/USGS_13_n40w089.tif"]
        gis = LambdaGISProcessor()
        out = gis.combine_rasters(rasters)
        self.assertTrue(out.endswith(".tif"))
        os.unlink(out)


if __name__ == "__main__":
    unittest.main()
