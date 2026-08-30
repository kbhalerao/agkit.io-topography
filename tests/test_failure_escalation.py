"""A failed job must escalate, and every failure class must escalate alike.

`process_payload` had two failure classes with opposite outcomes: an unknown
function name propagated, and a handler that threw was caught and turned into
the string ``"ERROR"``. The second class was deleted from the queue as though
it had succeeded, so a failed job was indistinguishable from a done one.

These tests pin the single rule that replaces the two: every job in the
message is attempted, then the message escalates if any of them did not land.

`geoworker._cleanup` is patched throughout — `process_payload` calls it with
``/tmp/*`` and it does not honour IN_TEST, so an unpatched call would wipe the
host's /tmp.
"""
import unittest
from unittest import mock


try:
    from app import geoworker
    _GEOWORKER_AVAILABLE = True
except Exception:
    _GEOWORKER_AVAILABLE = False


def _job(fn_name="elev_public_10m", field_id=1, metering=None):
    job = {
        "metadata": {
            "function_name": fn_name,
            "field_id": field_id,
            "site_prefix": "agkit",
            "field_boundary": {},
        },
        "post": {"output": []},
    }
    if metering:
        job["metering"] = metering
    return job


@unittest.skipUnless(_GEOWORKER_AVAILABLE, "osgeo/numpy not installed locally")
class EscalationTests(unittest.TestCase):
    """Every failure class leaves `process_payload` by raising."""

    def setUp(self):
        patcher = mock.patch.object(geoworker, "_cleanup")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, payload):
        return geoworker.process_payload(payload)

    def test_raising_handler_escalates(self):
        """The class that used to be swallowed."""
        with mock.patch.object(geoworker.LambdaGISProcessor, "elev_public_10m",
                               side_effect=RuntimeError("GDAL blew up")):
            with self.assertRaises(geoworker.JobFailed):
                self._run([_job()])

    def test_unknown_function_escalates(self):
        """The class that already escalated, unchanged."""
        with self.assertRaises(geoworker.JobFailed):
            self._run([_job(fn_name="no_such_function")])

    def test_postback_error_escalates(self):
        """topo's named silent-failure mode: work succeeded, postback got a non-2xx."""
        with mock.patch.object(geoworker.LambdaGISProcessor, "elev_public_10m",
                               return_value=["Error posting TIF file, status: 500."]):
            with self.assertRaises(geoworker.JobFailed):
                self._run([_job()])

    def test_success_does_not_escalate(self):
        with mock.patch.object(geoworker.LambdaGISProcessor, "elev_public_10m",
                               return_value=["TIF file posted successfully."]):
            results = self._run([_job()])
        self.assertEqual(len(results), 1)

    def test_failed_job_names_itself_in_the_error(self):
        """The DLQ message has to say which job failed, or it is a shrug."""
        with mock.patch.object(geoworker.LambdaGISProcessor, "elev_public_10m",
                               side_effect=RuntimeError("GDAL blew up")):
            with self.assertRaises(geoworker.JobFailed) as ctx:
                self._run([_job(field_id=4242)])
        message = str(ctx.exception)
        self.assertIn("elev_public_10m", message)
        self.assertIn("4242", message)


@unittest.skipUnless(_GEOWORKER_AVAILABLE, "osgeo/numpy not installed locally")
class SiblingJobTests(unittest.TestCase):
    """One message carries a whole bundle — the default is five jobs. A
    failure in one must not deny the other four their run."""

    def setUp(self):
        patcher = mock.patch.object(geoworker, "_cleanup")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_siblings_run_after_a_raising_job(self):
        ok = mock.Mock(return_value=["TIF file posted successfully."])
        with mock.patch.object(geoworker.LambdaGISProcessor, "elev_public_10m",
                               side_effect=RuntimeError("GDAL blew up")):
            with mock.patch.object(geoworker.LambdaGISProcessor,
                                   "slope_public_10m", ok):
                with self.assertRaises(geoworker.JobFailed):
                    geoworker.process_payload(
                        [_job("elev_public_10m"), _job("slope_public_10m")])
        ok.assert_called_once()

    def test_siblings_run_after_an_unknown_function(self):
        """The unknown-function class used to abandon the rest of the bundle."""
        ok = mock.Mock(return_value=["TIF file posted successfully."])
        with mock.patch.object(geoworker.LambdaGISProcessor, "slope_public_10m", ok):
            with self.assertRaises(geoworker.JobFailed):
                geoworker.process_payload(
                    [_job("no_such_function"), _job("slope_public_10m")])
        ok.assert_called_once()

    def test_every_failure_in_the_bundle_is_named(self):
        with mock.patch.object(geoworker.LambdaGISProcessor, "elev_public_10m",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(geoworker.JobFailed) as ctx:
                geoworker.process_payload(
                    [_job("elev_public_10m"), _job("no_such_function")])
        message = str(ctx.exception)
        self.assertIn("elev_public_10m", message)
        self.assertIn("no_such_function", message)


@unittest.skipUnless(_GEOWORKER_AVAILABLE, "osgeo/numpy not installed locally")
class BillingGuardTests(unittest.TestCase):
    """Escalation must not start billing work that did not land, and must not
    stop billing work that did. `_report_completion`'s `_job_succeeded` guard
    is what holds this — these fail if escalation is wired ahead of it."""

    METER = {"key_hash": "kh-1", "endpoint": "elevation-async"}

    def setUp(self):
        patcher = mock.patch.object(geoworker, "_cleanup")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_failed_job_is_not_billed(self):
        with mock.patch.object(geoworker.LambdaGISProcessor, "elev_public_10m",
                               side_effect=RuntimeError("boom")):
            with mock.patch("app.metering.report_usage") as report:
                with self.assertRaises(geoworker.JobFailed):
                    geoworker.process_payload([_job(metering=self.METER)])
        report.assert_not_called()

    def test_successful_sibling_is_still_billed_once(self):
        with mock.patch.object(geoworker.LambdaGISProcessor, "elev_public_10m",
                               side_effect=RuntimeError("boom")):
            with mock.patch.object(
                    geoworker.LambdaGISProcessor, "slope_public_10m",
                    return_value=["TIF file posted successfully."]):
                with mock.patch("app.metering.report_usage") as report:
                    with self.assertRaises(geoworker.JobFailed):
                        geoworker.process_payload([
                            _job("elev_public_10m", metering=self.METER),
                            _job("slope_public_10m", metering=self.METER),
                        ])
        report.assert_called_once_with("kh-1", "elevation-async")


@unittest.skipUnless(_GEOWORKER_AVAILABLE, "osgeo/numpy not installed locally")
class BatchItemFailureTests(unittest.TestCase):
    """The whole point of raising: `app.handler` reports the message as a
    batch item failure, SQS stops deleting it, and the redrive policy parks
    it on the DLQ. This walks the real `process_payload`, not a patched one."""

    def setUp(self):
        patcher = mock.patch.object(geoworker, "_cleanup")
        patcher.start()
        self.addCleanup(patcher.stop)
        from app import handler
        self.handler = handler

    def _event(self, jobs, message_id="m-1"):
        import json
        return {"Records": [{"messageId": message_id, "body": json.dumps(jobs)}]}

    def test_failed_job_is_reported_as_a_batch_item_failure(self):
        with mock.patch.object(geoworker.LambdaGISProcessor, "elev_public_10m",
                               side_effect=RuntimeError("GDAL blew up")):
            result = self.handler.handler(self._event([_job()]), context=None)
        self.assertEqual(
            result, {"batchItemFailures": [{"itemIdentifier": "m-1"}]})

    def test_successful_job_is_not_reported(self):
        with mock.patch.object(geoworker.LambdaGISProcessor, "elev_public_10m",
                               return_value=["TIF file posted successfully."]):
            result = self.handler.handler(self._event([_job()]), context=None)
        self.assertEqual(result, {"batchItemFailures": []})


if __name__ == "__main__":
    unittest.main()
