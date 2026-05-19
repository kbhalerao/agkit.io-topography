"""
Tests for the watershed-bounded flow domain.

`_watershed_extent` and the extent helpers are pure (osgeo only — no
GRASS). `BuildCommonCacheDomainTests` exercises `build_common_cache`'s
domain decisions with the DEM fetch and the GDAL clip patched out, so they
run in CI without DEM tiles or GRASS.

See docs/watershed-bounded-flow-feature.md.
"""
import json
import unittest
from unittest import mock


try:
    from app.geoworker import LambdaGISProcessor
    _GEOWORKER_AVAILABLE = True
except Exception:
    _GEOWORKER_AVAILABLE = False


def _box(x0, y0, x1, y1):
    """A closed polygon ring for the bbox (x0, y0)-(x1, y1)."""
    return [[x0, y0], [x0, y1], [x1, y1], [x1, y0], [x0, y0]]


def _fc(*rings):
    """A FeatureCollection of one Polygon feature per ring."""
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {},
             "geometry": {"type": "Polygon", "coordinates": [ring]}}
            for ring in rings
        ],
    }


# A small field in central IL.
FIELD = _fc(_box(-88.01, 40.01, -88.00, 40.02))


@unittest.skipUnless(_GEOWORKER_AVAILABLE, "osgeo not installed locally")
class WatershedExtentTests(unittest.TestCase):
    """`_watershed_extent` — parse a watershed_boundary FeatureCollection."""

    def test_none_boundary_returns_none(self):
        self.assertIsNone(LambdaGISProcessor._watershed_extent(None))

    def test_empty_collection_returns_none(self):
        self.assertIsNone(LambdaGISProcessor._watershed_extent(_fc()))

    def test_single_polygon_bbox(self):
        ext = LambdaGISProcessor._watershed_extent(
            _fc(_box(-89.0, 40.0, -88.0, 41.0)))
        self.assertEqual(
            tuple(round(v, 6) for v in ext), (-89.0, 40.0, -88.0, 41.0))

    def test_multi_feature_union(self):
        ext = LambdaGISProcessor._watershed_extent(_fc(
            _box(-89.0, 40.0, -88.5, 40.5),
            _box(-88.6, 40.4, -88.0, 41.0),
        ))
        self.assertEqual(
            tuple(round(v, 6) for v in ext), (-89.0, 40.0, -88.0, 41.0))

    def test_json_string_form_is_parsed(self):
        ext = LambdaGISProcessor._watershed_extent(
            json.dumps(_fc(_box(-89.0, 40.0, -88.0, 41.0))))
        self.assertIsNotNone(ext)
        self.assertEqual(round(ext[0], 6), -89.0)

    def test_malformed_string_returns_none(self):
        self.assertIsNone(LambdaGISProcessor._watershed_extent("not-json"))

    def test_feature_without_geometry_returns_none(self):
        fc = {"type": "FeatureCollection",
              "features": [{"type": "Feature", "properties": {}}]}
        self.assertIsNone(LambdaGISProcessor._watershed_extent(fc))


@unittest.skipUnless(_GEOWORKER_AVAILABLE, "osgeo not installed locally")
class ExtentHelperTests(unittest.TestCase):

    def test_buffer_extent_pads_all_sides(self):
        self.assertEqual(
            LambdaGISProcessor._buffer_extent((-88.0, 40.0, -87.0, 41.0), 0.5),
            (-88.5, 39.5, -86.5, 41.5))

    def test_union_extent_encloses_both(self):
        self.assertEqual(
            LambdaGISProcessor._union_extent(
                (-89.0, 40.0, -88.0, 41.0), (-88.5, 39.0, -87.0, 40.5)),
            (-89.0, 39.0, -87.0, 41.0))


@unittest.skipUnless(_GEOWORKER_AVAILABLE, "osgeo not installed locally")
class BuildCommonCacheDomainTests(unittest.TestCase):
    """`build_common_cache` picks the right DEM domains. The DEM fetch and
    the GDAL clip are patched, so no tiles or GRASS are needed — only ogr,
    for the real field-geometry parsing."""

    def _build(self, watershed_boundary):
        gis = LambdaGISProcessor()
        payload = {"metadata": {
            "field_boundary": FIELD,
            "watershed_boundary": watershed_boundary,
            "field_id": 1,
        }}
        clip_calls = []

        def fake_clip(dem, extent):
            clip_calls.append(tuple(round(v, 6) for v in extent))
            return f"clip_{len(clip_calls)}.tif"

        with mock.patch.object(gis, "get_shapefile_from_geojson_ogr",
                               return_value="field.shp"), \
             mock.patch.object(gis, "get_elev_raster",
                               return_value="dem.tif") as fetch, \
             mock.patch.object(gis, "gdal_clip_to_bounds",
                               side_effect=fake_clip):
            gis.build_common_cache(payload)
        return gis, fetch, clip_calls

    def test_no_boundary_falls_back_to_field_buffer(self):
        gis, fetch, clips = self._build(None)
        self.assertEqual(gis.cache["flow_domain"], "field_buffer")
        # watershed_raster aliases the field-buffered clip — one clip only.
        self.assertIs(
            gis.cache["watershed_raster"], gis.cache["clipped_raster"])
        self.assertEqual(len(clips), 1)
        b = LambdaGISProcessor._FALLBACK_BUFFER_DEG
        self.assertEqual(clips[0], (
            round(-88.01 - b, 6), round(40.01 - b, 6),
            round(-88.00 + b, 6), round(40.02 + b, 6),
        ))

    def test_watershed_boundary_sets_watershed_domain(self):
        ws = _fc(_box(-88.20, 39.90, -87.80, 40.30))
        gis, fetch, clips = self._build(ws)
        self.assertEqual(gis.cache["flow_domain"], "watershed")
        self.assertIsNot(
            gis.cache["watershed_raster"], gis.cache["clipped_raster"])
        # Two clips: field-buffered, then the watershed bbox.
        self.assertEqual(len(clips), 2)
        self.assertEqual(clips[1], (-88.20, 39.90, -87.80, 40.30))
        # DEM fetched once, at the union of both extents (= watershed here).
        fetch.assert_called_once()
        (fetch_extent,) = fetch.call_args.args
        self.assertEqual(tuple(round(v, 6) for v in fetch_extent),
                         (-88.20, 39.90, -87.80, 40.30))

    def test_watershed_smaller_than_field_still_fetches_union(self):
        # A hand-drawn area smaller than the field+buffer — a literal,
        # under-drawn compute domain. The DEM fetch must still cover the
        # field-buffered clip.
        ws = _fc(_box(-88.005, 40.012, -88.002, 40.018))
        gis, fetch, clips = self._build(ws)
        self.assertEqual(gis.cache["flow_domain"], "watershed")
        b = LambdaGISProcessor._FALLBACK_BUFFER_DEG
        (fetch_extent,) = fetch.call_args.args
        self.assertLessEqual(fetch_extent[0], -88.01 - b)
        self.assertLessEqual(fetch_extent[1], 40.01 - b)
        self.assertGreaterEqual(fetch_extent[2], -88.00 + b)
        self.assertGreaterEqual(fetch_extent[3], 40.02 + b)
        # The flow clip is the small drawn polygon, as drawn.
        self.assertEqual(clips[1], (-88.005, 40.012, -88.002, 40.018))

    def test_json_string_boundary_accepted(self):
        ws = json.dumps(_fc(_box(-88.20, 39.90, -87.80, 40.30)))
        gis, _, _ = self._build(ws)
        self.assertEqual(gis.cache["flow_domain"], "watershed")

    def test_null_boundary_treated_as_unset(self):
        gis, _, clips = self._build(None)
        # Mirrors a payload with "watershed_boundary": null.
        self.assertEqual(gis.cache["flow_domain"], "field_buffer")
        self.assertEqual(len(clips), 1)


if __name__ == "__main__":
    unittest.main()
