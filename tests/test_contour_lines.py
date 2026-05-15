"""
Tests for the contour-lines feature.

Two layers:

* `ContourTaggingTests` runs anywhere — pure JSON post-processing.
* `ContourGenerationTests` requires GRASS on PATH and the test elevation
  tif fixture; mirrors the `test_grass_watershed.py` skip pattern. CI
  inside the Lambda image exercises it.
"""
import json
import os
import shutil
import tempfile
import unittest


try:
    from app.grass_handler import get_contour_lines
    _GRASS_AVAILABLE = True
except Exception:
    _GRASS_AVAILABLE = False

try:
    from app.geoworker import LambdaGISProcessor
    _GEOWORKER_AVAILABLE = True
except Exception:
    _GEOWORKER_AVAILABLE = False


class ContourTaggingTests(unittest.TestCase):
    """`_tag_contour_features` is pure JSON manipulation — exercise it directly."""

    @unittest.skipUnless(_GEOWORKER_AVAILABLE, "osgeo/numpy not installed locally")
    def test_renames_level_and_tags_is_index(self):
        raw = {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature",
                 "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
                 "properties": {"level": 100.0, "cat": 1}},
                {"type": "Feature",
                 "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
                 "properties": {"level": 102.0, "cat": 2}},
                {"type": "Feature",
                 "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
                 "properties": {"level": 104.0, "cat": 3}},
                {"type": "Feature",
                 "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
                 "properties": {"level": 110.0, "cat": 4}},
            ],
        }
        with tempfile.NamedTemporaryFile(suffix=".geojson", mode="w", delete=False) as fh:
            json.dump(raw, fh)
            path = fh.name
        try:
            LambdaGISProcessor._tag_contour_features(path, index_step=10)
            with open(path) as f:
                tagged = json.load(f)
        finally:
            os.unlink(path)

        levels = [f["properties"]["level_ft"] for f in tagged["features"]]
        indices = [f["properties"]["is_index"] for f in tagged["features"]]
        self.assertEqual(levels, [100.0, 102.0, 104.0, 110.0])
        self.assertEqual(indices, [True, False, False, True])
        # `level` should be gone — only the renamed key remains.
        for feature in tagged["features"]:
            self.assertNotIn("level", feature["properties"])


@unittest.skipUnless(
    _GRASS_AVAILABLE and os.path.exists("tests/elevation_public_10m.tif"),
    "GRASS or test elevation tif unavailable",
)
class ContourGenerationTests(unittest.TestCase):
    """End-to-end: feed a real DEM tif through r.contour."""

    OUTDIR = "tests/out_contours"

    def setUp(self):
        os.makedirs(self.OUTDIR, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.OUTDIR, ignore_errors=True)

    def test_get_contour_lines_emits_valid_geojson_with_level_property(self):
        path = get_contour_lines(
            "tests/elevation_public_10m.tif", self.OUTDIR, step_ft=2,
        )
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            data = json.load(f)

        self.assertEqual(data.get("type"), "FeatureCollection")
        features = data.get("features", [])
        self.assertGreater(len(features), 0, "no contour features generated")

        # Every feature should be a LineString-ish geometry with a level.
        for feature in features:
            geom = feature.get("geometry") or {}
            self.assertIn(
                geom.get("type"),
                ("LineString", "MultiLineString"),
                f"unexpected geometry type: {geom.get('type')}",
            )
            self.assertIn("level", feature.get("properties", {}))

    @unittest.skipUnless(_GEOWORKER_AVAILABLE, "osgeo/numpy not installed locally")
    def test_after_tagging_every_feature_has_level_ft_and_is_index(self):
        path = get_contour_lines(
            "tests/elevation_public_10m.tif", self.OUTDIR, step_ft=2,
        )
        LambdaGISProcessor._tag_contour_features(path, index_step=10)
        with open(path) as f:
            data = json.load(f)

        for feature in data["features"]:
            props = feature["properties"]
            self.assertIn("level_ft", props)
            self.assertIn("is_index", props)
            self.assertNotIn("level", props)

            level_ft = props["level_ft"]
            self.assertAlmostEqual(level_ft % 2, 0, places=4,
                                   msg=f"non-2ft contour: {level_ft}")
            self.assertEqual(props["is_index"], (level_ft % 10 == 0))


if __name__ == "__main__":
    unittest.main()
