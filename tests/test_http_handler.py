"""
Tests for the sync elevation HTTP handler + x402 metering gate.

These are pure — no GDAL. `app.metering` imports only `requests`, and the
handler lazy-imports the renderer, so we stub `app.sync_elevation` in
`sys.modules` to exercise the gate / parse / billing / response-shaping logic
locally. The actual DEM render is covered by `test_sync_elevation` (in-image).
"""
import base64
import json
import sys
import types
import unittest
from unittest import mock

from app import metering

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-bytes"

# One monitor endpoint + one enforce endpoint, plus three consumers.
GOOD = "good-key"       # active, permitted for both
ELEV_ONLY = "elev-only"  # active, permitted for elevation only
INACTIVE = "inactive-key"  # inactive

CONFIG_BLOB = {
    "config_version": 7,
    "product_groups": [{
        "id": "topo",
        "enabled": True,
        "endpoints": [
            {"id": "elevation", "method": "POST",
             "path": "/x402/v1/topo/elevation", "enforcement": "monitor"},
            {"id": "enforced", "method": "POST",
             "path": "/x402/v1/topo/enforced", "enforcement": "enforce"},
        ],
    }],
    "consumers": [
        {"key_hash": metering.hash_key(GOOD), "consumer_name": "acme",
         "is_active": True, "permitted_endpoints": ["elevation", "enforced"]},
        {"key_hash": metering.hash_key(ELEV_ONLY), "consumer_name": "beta",
         "is_active": True, "permitted_endpoints": ["elevation"]},
        {"key_hash": metering.hash_key(INACTIVE), "consumer_name": "gamma",
         "is_active": False, "permitted_endpoints": ["enforced"]},
    ],
}


def _bearer(key):
    return {"authorization": f"Bearer {key}"}


class MeteringGateTests(unittest.TestCase):
    def setUp(self):
        # Enable metering with the topo prefix and install the config directly.
        self.patchers = [
            mock.patch.object(metering, "ENABLED", True),
            mock.patch.object(metering, "CATALOG_PATH_PREFIX", "/x402/v1/topo"),
        ]
        for p in self.patchers:
            p.start()
        metering.install_config_for_tests(CONFIG_BLOB)

    def tearDown(self):
        for p in self.patchers:
            p.stop()
        metering._cache = None

    def test_disabled_serves(self):
        with mock.patch.object(metering, "ENABLED", False):
            d = metering.gate("POST", "/elevation", _bearer(GOOD))
        self.assertEqual(d.action, "serve")

    def test_options_serves(self):
        self.assertEqual(metering.gate("OPTIONS", "/elevation", {}).action, "serve")

    def test_unmetered_path_serves(self):
        self.assertEqual(metering.gate("POST", "/nope", {}).action, "serve")

    def test_monitor_with_consumer_records(self):
        d = metering.gate("POST", "/elevation", _bearer(GOOD))
        self.assertEqual(d.action, "serve_and_record")
        self.assertEqual(d.endpoint_slug, "elevation")
        self.assertEqual(d.record_key_hash, metering.hash_key(GOOD))

    def test_monitor_without_credential_serves_unbilled(self):
        d = metering.gate("POST", "/elevation", {})
        self.assertEqual(d.action, "serve_and_record")
        self.assertIsNone(d.record_key_hash)

    def test_enforce_no_credential_401(self):
        d = metering.gate("POST", "/enforced", {})
        self.assertEqual(d.action, "reject")
        self.assertEqual(d.error["status"], 401)

    def test_enforce_unknown_credential_401(self):
        d = metering.gate("POST", "/enforced", _bearer("stranger"))
        self.assertEqual(d.error["status"], 401)

    def test_enforce_inactive_credential_401(self):
        d = metering.gate("POST", "/enforced", _bearer(INACTIVE))
        self.assertEqual(d.error["status"], 401)

    def test_enforce_unpermitted_403(self):
        d = metering.gate("POST", "/enforced", _bearer(ELEV_ONLY))
        self.assertEqual(d.action, "reject")
        self.assertEqual(d.error["status"], 403)

    def test_enforce_permitted_records(self):
        d = metering.gate("POST", "/enforced", _bearer(GOOD))
        self.assertEqual(d.action, "serve_and_record")
        self.assertEqual(d.record_key_hash, metering.hash_key(GOOD))


class HttpHandlerTests(unittest.TestCase):
    def setUp(self):
        # Stub the GDAL-backed renderer so the handler is importable/runnable
        # without GDAL.
        self.fake = types.ModuleType("app.sync_elevation")
        self.fake.render_elevation = mock.Mock(return_value={
            "png": PNG, "extent": [-91.5, 41.5, -91.4, 41.6],
            "width": 10, "height": 10,
        })
        sys.modules["app.sync_elevation"] = self.fake
        from app import http_handler
        self.handler = http_handler.handler

    def tearDown(self):
        sys.modules.pop("app.sync_elevation", None)

    @staticmethod
    def _event(body, method="POST", path="/elevation", headers=None):
        return {
            "requestContext": {"http": {"method": method, "path": path}},
            "headers": headers or {},
            "body": body if isinstance(body, str) else json.dumps(body),
            "isBase64Encoded": False,
        }

    def test_options_preflight(self):
        resp = self.handler(self._event("", method="OPTIONS"), None)
        self.assertEqual(resp["statusCode"], 204)

    def test_serve_returns_png(self):
        with mock.patch.object(metering, "gate",
                               return_value=metering.Decision("serve")):
            resp = self.handler(self._event({"type": "Polygon", "coordinates": []}), None)
        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(resp["headers"]["content-type"], "image/png")
        self.assertTrue(resp["isBase64Encoded"])
        self.assertEqual(base64.b64decode(resp["body"]), PNG)
        self.assertEqual(json.loads(resp["headers"]["x-extent"]), [-91.5, 41.5, -91.4, 41.6])

    def test_serve_and_record_bills(self):
        decision = metering.Decision("serve_and_record", "elevation", "abc123")
        with mock.patch.object(metering, "gate", return_value=decision), \
                mock.patch.object(metering, "report_usage") as report:
            resp = self.handler(self._event({"type": "Polygon", "coordinates": []}), None)
        self.assertEqual(resp["statusCode"], 200)
        report.assert_called_once_with("abc123", "elevation")

    def test_reject_short_circuits_before_render(self):
        decision = metering.Decision("reject", error={
            "status": 402, "code": "payment_required", "detail": "pay up"})
        with mock.patch.object(metering, "gate", return_value=decision):
            resp = self.handler(self._event({"type": "Polygon"}), None)
        self.assertEqual(resp["statusCode"], 402)
        self.fake.render_elevation.assert_not_called()

    def test_bad_body_400(self):
        with mock.patch.object(metering, "gate",
                               return_value=metering.Decision("serve")):
            resp = self.handler(self._event("not-json"), None)
        self.assertEqual(resp["statusCode"], 400)

    def test_render_valueerror_422(self):
        self.fake.render_elevation.side_effect = ValueError("too big")
        with mock.patch.object(metering, "gate",
                               return_value=metering.Decision("serve")):
            resp = self.handler(self._event({"type": "Polygon", "coordinates": []}), None)
        self.assertEqual(resp["statusCode"], 422)

    def test_base64_body_decoded(self):
        raw = json.dumps({"type": "Polygon", "coordinates": []})
        event = self._event(base64.b64encode(raw.encode()).decode())
        event["isBase64Encoded"] = True
        with mock.patch.object(metering, "gate",
                               return_value=metering.Decision("serve")):
            resp = self.handler(event, None)
        self.assertEqual(resp["statusCode"], 200)


if __name__ == "__main__":
    unittest.main()
