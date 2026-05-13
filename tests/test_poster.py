"""
Tests for the signed-URL postback client.

Mocks `requests.post` so these tests run without network and without
GDAL/GRASS. They verify wire format: multipart for rasters, JSON for
scalars; no Authorization header in either case.
"""
import os
import tempfile
import unittest
from unittest import mock


class PostRasterTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tif")
        tmp.write(b"\x00\x01\x02not-a-real-tif")
        tmp.close()
        self.tif_path = tmp.name

    def tearDown(self):
        try:
            os.unlink(self.tif_path)
        except OSError:
            pass

    def test_post_raster_uses_multipart_with_file_parameter_layer(self):
        from app import poster
        with mock.patch("app.poster.requests.post") as p:
            p.return_value = mock.Mock(ok=True, status_code=201)
            poster.post_raster(
                post_url="https://api.test/api/v1/topography/postback/raster/1/tok/",
                filepath=self.tif_path,
                parameter="watershed_length_slope_raster",
                layer="USGS Length Slope",
                filename="length_slope.tif",
            )
        p.assert_called_once()
        kwargs = p.call_args.kwargs
        self.assertIn("files", kwargs)
        self.assertIn("data", kwargs)
        self.assertEqual(kwargs["data"]["parameter"], "watershed_length_slope_raster")
        self.assertEqual(kwargs["data"]["layer"], "USGS Length Slope")
        self.assertEqual(kwargs["files"]["file"][0], "length_slope.tif")
        # No Authorization header on the signed-URL path.
        self.assertNotIn("headers", kwargs)

    def test_post_raster_uses_basename_when_no_filename(self):
        from app import poster
        with mock.patch("app.poster.requests.post") as p:
            p.return_value = mock.Mock(ok=True, status_code=201)
            poster.post_raster(
                post_url="https://api.test/r/1/t/",
                filepath=self.tif_path,
                parameter="elevation_public_10m",
                layer="",
            )
        kwargs = p.call_args.kwargs
        self.assertEqual(
            kwargs["files"]["file"][0], os.path.basename(self.tif_path),
        )


class PostScalarTests(unittest.TestCase):
    def test_post_scalar_uses_json_body(self):
        from app import poster
        with mock.patch("app.poster.requests.post") as p:
            p.return_value = mock.Mock(ok=True, status_code=200)
            poster.post_scalar(
                scalar_url="https://api.test/api/v1/topography/postback/scalar/1/tok/",
                parameter="watershed_length_slope",
                value=12.34,
                units="pixels",
                lambda_function="watershed_length_slope",
                source="usgs_10m",
            )
        kwargs = p.call_args.kwargs
        self.assertIn("json", kwargs)
        body = kwargs["json"]
        self.assertEqual(body["parameter"], "watershed_length_slope")
        self.assertEqual(body["value"], 12.34)
        self.assertEqual(body["units"], "pixels")
        self.assertEqual(body["source"], "usgs_10m")
        # JSON path: no multipart and no headers either.
        self.assertNotIn("files", kwargs)


if __name__ == "__main__":
    unittest.main()
