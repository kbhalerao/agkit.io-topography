"""
Tests for the SQS event handler.

`app.geoworker.process_payload` is patched per-test, so these don't
touch the real GDAL pipeline. They verify dispatch shape, partial-batch
failure reporting, and body parsing edge cases.
"""
import json
import os
import unittest
from unittest import mock


os.environ.setdefault("IN_TEST", "true")

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _load_fixture(name: str) -> dict:
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


class HandlerDispatchTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("app.geoworker.process_payload", return_value=["ok"])
        self.process_payload = patcher.start()
        self.addCleanup(patcher.stop)
        from app import handler
        self.handler = handler

    def test_inline_list_body_dispatched(self):
        event = _load_fixture("sqs_event.json")
        result = self.handler.handler(event, context=None)
        self.assertEqual(result, {"batchItemFailures": []})
        self.process_payload.assert_called_once()
        (payload,) = self.process_payload.call_args.args
        self.assertIsInstance(payload, list)
        self.assertEqual(payload[0]["metadata"]["function_name"], "elev_public_10m")

    def test_scalar_event_dispatched(self):
        event = _load_fixture("sqs_event_scalar.json")
        result = self.handler.handler(event, context=None)
        self.assertEqual(result, {"batchItemFailures": []})
        (payload,) = self.process_payload.call_args.args
        self.assertEqual(payload[0]["metadata"]["function_name"], "watershed_length_slope")
        self.assertIn("scalar_url", payload[0]["post"])

    def test_single_dict_body_wrapped(self):
        event = {"Records": [{
            "messageId": "m1",
            "body": json.dumps({
                "metadata": {"function_name": "elev_public_10m",
                             "field_boundary": {}, "field_id": 1},
                "post": {"output": []},
            }),
        }]}
        self.handler.handler(event, context=None)
        (payload,) = self.process_payload.call_args.args
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["metadata"]["function_name"], "elev_public_10m")

    def test_exception_in_one_record_is_partial_failure(self):
        event = {"Records": [
            {"messageId": "ok-1", "body": "[]"},
            {"messageId": "bad-2", "body": "not-json"},
        ]}
        result = self.handler.handler(event, context=None)
        self.assertEqual(
            result, {"batchItemFailures": [{"itemIdentifier": "bad-2"}]},
        )

    def test_empty_event(self):
        result = self.handler.handler({"Records": []}, context=None)
        self.assertEqual(result, {"batchItemFailures": []})


if __name__ == "__main__":
    unittest.main()
