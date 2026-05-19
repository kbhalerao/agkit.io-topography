"""
Tests for the MFD flow-lines feature.

Three layers:

* `DPSimplifierTests` — pure Python DP on (length, elevation) curves.
* `ProfileShapeTests` — uniform / concave / convex / complex classifier.
* `SeedPickingTests` — non-max suppression on flow_acc array.
* `FlowLinesIntegrationTests` — runs the full GRASS pipeline against the
  test elevation tif; skipped when GRASS or osgeo isn't available locally.
"""
import unittest

try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except Exception:
    _NUMPY_AVAILABLE = False

try:
    from app.geoworker import LambdaGISProcessor
    _GEOWORKER_AVAILABLE = True
except Exception:
    _GEOWORKER_AVAILABLE = False

try:
    from app.grass_handler import (
        _divide_mask,
        _path_length_cells,
        _pick_seeds,
        _trace_d8_path,
        get_mfd_flowlines_raw,
    )
    _GRASS_HELPERS_AVAILABLE = True
except Exception:
    _GRASS_HELPERS_AVAILABLE = False


@unittest.skipUnless(_GEOWORKER_AVAILABLE, "osgeo/numpy not installed locally")
class DPSimplifierTests(unittest.TestCase):
    """Pure-Python — independent of GRASS / osgeo runtime."""

    def test_straight_line_collapses_to_endpoints(self):
        # 10 points on a perfect line — every point coplanar with the line p0→pN.
        pts = [(i * 10.0, i * 0.5) for i in range(10)]
        result = LambdaGISProcessor._dp_simplify(pts, epsilon=0.1)
        self.assertEqual(result, [pts[0], pts[-1]])

    def test_curved_line_keeps_apex_within_epsilon(self):
        # V-shape: an obvious apex at index 2 with perpendicular distance 5.
        pts = [(0, 0), (10, 0), (20, 5), (30, 0), (40, 0)]
        result = LambdaGISProcessor._dp_simplify(pts, epsilon=1.0)
        self.assertIn((20.0, 5.0), [(float(x), float(y)) for x, y in result])

    def test_simplify_profile_caps_at_max_segments(self):
        # Noisy descending profile with many small bumps.
        pts = []
        cum = 0.0
        elev = 1000.0
        for i in range(60):
            cum += 10.0
            elev -= 0.5 + (0.3 if i % 2 == 0 else -0.3)  # tiny oscillation
            pts.append((cum, elev))
        segments = LambdaGISProcessor._simplify_profile(
            pts, epsilon_ft=0.5, max_segments=4, min_seg_length_ft=20,
        )
        self.assertLessEqual(len(segments), 4)
        self.assertGreater(len(segments), 0)

    def test_simplify_profile_merges_short_segments(self):
        # A profile that would naturally produce one very-short segment.
        # The DP keeps the apex; min-length merge should fold it away.
        pts = [(0, 100), (10, 100), (12, 99.5), (100, 90)]
        segments = LambdaGISProcessor._simplify_profile(
            pts, epsilon_ft=0.1, max_segments=10, min_seg_length_ft=50,
        )
        for seg in segments:
            self.assertGreaterEqual(seg["length_ft"], 50)

    def test_simplify_profile_empty_input(self):
        self.assertEqual(LambdaGISProcessor._simplify_profile([]), [])
        self.assertEqual(LambdaGISProcessor._simplify_profile([(0, 100)]), [])


@unittest.skipUnless(_GEOWORKER_AVAILABLE, "osgeo/numpy not installed locally")
class ProfileShapeTests(unittest.TestCase):

    def test_single_segment_is_uniform(self):
        segs = [{"length_ft": 100, "slope_pct": 3.0}]
        self.assertEqual(LambdaGISProcessor._classify_profile_shape(segs), "uniform")

    def test_low_variance_is_uniform(self):
        segs = [
            {"length_ft": 100, "slope_pct": 3.0},
            {"length_ft": 100, "slope_pct": 3.1},
            {"length_ft": 100, "slope_pct": 2.95},
        ]
        self.assertEqual(LambdaGISProcessor._classify_profile_shape(segs), "uniform")

    def test_monotone_increasing_slope_is_convex(self):
        segs = [
            {"length_ft": 100, "slope_pct": 1.0},
            {"length_ft": 100, "slope_pct": 3.0},
            {"length_ft": 100, "slope_pct": 6.0},
        ]
        self.assertEqual(LambdaGISProcessor._classify_profile_shape(segs), "convex")

    def test_monotone_decreasing_slope_is_concave(self):
        segs = [
            {"length_ft": 100, "slope_pct": 6.0},
            {"length_ft": 100, "slope_pct": 3.0},
            {"length_ft": 100, "slope_pct": 1.0},
        ]
        self.assertEqual(LambdaGISProcessor._classify_profile_shape(segs), "concave")

    def test_multiple_sign_changes_is_complex(self):
        segs = [
            {"length_ft": 100, "slope_pct": 2.0},
            {"length_ft": 100, "slope_pct": 6.0},
            {"length_ft": 100, "slope_pct": 1.0},
            {"length_ft": 100, "slope_pct": 4.0},
        ]
        self.assertEqual(LambdaGISProcessor._classify_profile_shape(segs), "complex")

    def test_empty_segments_is_uniform(self):
        self.assertEqual(LambdaGISProcessor._classify_profile_shape([]), "uniform")


@unittest.skipUnless(_NUMPY_AVAILABLE and _GRASS_HELPERS_AVAILABLE,
                     "numpy or grass_handler helpers unavailable")
class SeedPickingTests(unittest.TestCase):

    def test_picks_top_n_by_value(self):
        arr = np.zeros((10, 10))
        arr[0, 0] = 100
        arr[5, 5] = 200
        arr[9, 9] = 150
        seeds = _pick_seeds(arr, max_lines=3, min_flow_acc=50, nms_radius_cells=1)
        self.assertEqual([s[2] for s in seeds], [200.0, 150.0, 100.0])

    def test_nms_rejects_neighbors_within_radius(self):
        arr = np.zeros((10, 10))
        arr[5, 5] = 200
        arr[5, 6] = 195   # adjacent — should be suppressed at radius=1
        arr[0, 0] = 100   # far away — should survive
        seeds = _pick_seeds(arr, max_lines=3, min_flow_acc=50, nms_radius_cells=1)
        seed_values = [s[2] for s in seeds]
        self.assertIn(200.0, seed_values)
        self.assertNotIn(195.0, seed_values)
        self.assertIn(100.0, seed_values)

    def test_below_threshold_returns_empty(self):
        arr = np.full((10, 10), 10.0)
        seeds = _pick_seeds(arr, max_lines=5, min_flow_acc=50, nms_radius_cells=1)
        self.assertEqual(seeds, [])

    def test_max_lines_cap_respected(self):
        arr = np.zeros((20, 20))
        # Place 10 well-separated peaks
        for i in range(10):
            arr[i * 2, i * 2] = 100 + i
        seeds = _pick_seeds(arr, max_lines=3, min_flow_acc=50, nms_radius_cells=1)
        self.assertEqual(len(seeds), 3)


@unittest.skipUnless(_NUMPY_AVAILABLE and _GRASS_HELPERS_AVAILABLE,
                     "numpy or grass_handler helpers unavailable")
class D8WalkTests(unittest.TestCase):
    """Divide detection and downslope D8 walk — the RUSLE2 re-seed core."""

    def test_path_length_orthogonal_and_diagonal(self):
        self.assertAlmostEqual(
            _path_length_cells([(0, 0), (0, 1), (0, 2)]), 2.0)
        self.assertAlmostEqual(
            _path_length_cells([(0, 0), (1, 1)]), 1.41421356, places=5)

    def test_divide_mask_flags_only_the_unfed_cell(self):
        # A 1x3 row, every cell draining East (code 8). Nothing drains into
        # the westmost cell — it is the lone divide.
        drainage = np.array([[8, 8, 8]], dtype=float)
        mask = _divide_mask(drainage)
        self.assertTrue(mask[0, 0])
        self.assertFalse(mask[0, 1])
        self.assertFalse(mask[0, 2])

    def test_trace_stops_at_channel_head(self):
        # Row of cells draining East; accumulation climbs 1..5. The walk
        # halts on entering the first cell at/above the channel threshold.
        drainage = np.array([[8, 8, 8, 8, 8]], dtype=float)
        flow_acc = np.array([[1, 2, 3, 4, 5]], dtype=float)
        path = _trace_d8_path(drainage, flow_acc, (0, 0),
                              channel_threshold=4, max_steps=50)
        self.assertEqual(path, [(0, 0), (0, 1), (0, 2), (0, 3)])

    def test_trace_stops_at_raster_edge(self):
        # No cell reaches the threshold — the walk runs to the edge.
        drainage = np.array([[8, 8, 8]], dtype=float)
        flow_acc = np.array([[1, 1, 1]], dtype=float)
        path = _trace_d8_path(drainage, flow_acc, (0, 0),
                              channel_threshold=999, max_steps=50)
        self.assertEqual(path, [(0, 0), (0, 1), (0, 2)])


@unittest.skipUnless(
    _GRASS_HELPERS_AVAILABLE
    and _GEOWORKER_AVAILABLE
    and __import__("os").path.exists("tests/elevation_public_10m.tif"),
    "GRASS or test elevation tif unavailable",
)
class FlowLinesIntegrationTests(unittest.TestCase):
    """End-to-end: real DEM → GRASS pipeline → raw line records."""

    OUTDIR = "tests/out_flowlines"

    def setUp(self):
        import os
        os.makedirs(self.OUTDIR, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.OUTDIR, ignore_errors=True)

    def test_pipeline_returns_well_formed_lines(self):
        raw = get_mfd_flowlines_raw(
            "tests/elevation_public_10m.tif",
            self.OUTDIR,
            options={
                "max_lines": 3,
                "min_flow_accumulation_cells": 50,
                "min_line_length_ft": 50,
            },
        )
        # Either zero (flat field) or up to max_lines.
        self.assertLessEqual(len(raw), 3)
        for line in raw:
            self.assertIn("rank", line)
            self.assertIn("flow_accumulation_cells", line)
            self.assertTrue(line["line_wkt"].startswith("LINESTRING"))
            self.assertTrue(
                line["zone_wkt"].startswith("POLYGON")
                or line["zone_wkt"].startswith("MULTIPOLYGON")
            )
            self.assertIsInstance(line["profile_vertices_m"], list)


if __name__ == "__main__":
    unittest.main()
