"""Record-on-completion: the SQS worker bills a metered async job only after
its work + postback succeed.

Imports `app.geoworker` (osgeo/numpy). Skipped locally when those aren't
present; CI inside the Lambda image exercises it.
"""
import unittest
from unittest import mock


try:
    from app import geoworker
    _GEOWORKER_AVAILABLE = True
except Exception:
    _GEOWORKER_AVAILABLE = False


@unittest.skipUnless(_GEOWORKER_AVAILABLE, "osgeo/numpy not installed locally")
class JobSucceededTests(unittest.TestCase):
    def test_exception_marker_is_failure(self):
        self.assertFalse(geoworker._job_succeeded("ERROR"))

    def test_all_posted_ok_is_success(self):
        self.assertTrue(geoworker._job_succeeded(
            ["PNG file posted successfully.", "TIF file posted successfully."]))

    def test_any_postback_error_is_failure(self):
        self.assertFalse(geoworker._job_succeeded(
            ["PNG file posted successfully.", "Error posting TIF file, status: 500."]))

    def test_unknown_shape_fails_open(self):
        # A shape we don't recognize is treated as success (metering posture).
        self.assertTrue(geoworker._job_succeeded([("fn", "/tmp/out.tif")]))


@unittest.skipUnless(_GEOWORKER_AVAILABLE, "osgeo/numpy not installed locally")
class ReportCompletionTests(unittest.TestCase):
    METER = {"key_hash": "kh-1", "endpoint": "elevation-async"}

    def test_success_reports(self):
        job = {"metadata": {}, "metering": self.METER}
        with mock.patch("app.metering.report_usage") as report:
            geoworker._report_completion(job, ["PNG file posted successfully."])
        report.assert_called_once_with("kh-1", "elevation-async")

    def test_failure_does_not_report(self):
        job = {"metadata": {}, "metering": self.METER}
        with mock.patch("app.metering.report_usage") as report:
            geoworker._report_completion(job, "ERROR")
        report.assert_not_called()

    def test_no_metering_block_does_not_report(self):
        with mock.patch("app.metering.report_usage") as report:
            geoworker._report_completion({"metadata": {}}, ["posted successfully."])
        report.assert_not_called()

    def test_metering_block_without_key_skipped(self):
        job = {"metadata": {}, "metering": {"key_hash": None, "endpoint": "x"}}
        with mock.patch("app.metering.report_usage") as report:
            geoworker._report_completion(job, ["posted successfully."])
        report.assert_not_called()

    def test_report_failure_is_swallowed(self):
        job = {"metadata": {}, "metering": self.METER}
        with mock.patch("app.metering.report_usage",
                        side_effect=RuntimeError("x402 down")):
            geoworker._report_completion(job, ["posted successfully."])  # no raise


if __name__ == "__main__":
    unittest.main()
