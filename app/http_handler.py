"""API Gateway HTTP handler for the synchronous elevation endpoint.

Entrypoint: `app.http_handler.handler`. This is the SECOND trigger on the same
container image as the SQS worker (`app.handler.handler`) — the image already
carries GDAL, so a sync HTTP surface is just another handler. Deploy it as its
own Lambda function (own memory/timeout/concurrency), fronted by API Gateway
HTTP API with a Cloudflare CNAME (topo.agkit.io), in us-west-2.

Flow, per request:
  1. x402 gate (app.metering) — mirror of prismuserv's metering decision table.
     Rejects (401/403) short-circuit before any DEM work.
  2. render_elevation(boundary) — boundary GeoJSON in, colorized PNG out.
  3. On a 2xx with a billable consumer, POST one usage event to x402.

The Lambda self-bills (no gateway in front doing it): the gate resolves the
consumer from the cached x402 catalog config and the usage POST records the
call. See app.metering for the config/usage contracts.
"""
from __future__ import annotations

import base64
import json
import logging

from app import metering

logger = logging.getLogger(__name__)

_CORS = {
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "POST,OPTIONS",
    "access-control-allow-headers": "content-type,authorization",
}


def _json_response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", **_CORS},
        "isBase64Encoded": False,
        "body": json.dumps(body),
    }


def _request(event: dict) -> tuple[str, str, dict, str | None]:
    """(method, path, headers, raw_body) from an API Gateway v2 event."""
    ctx = (event.get("requestContext") or {}).get("http") or {}
    method = ctx.get("method") or event.get("httpMethod") or "POST"
    path = ctx.get("path") or event.get("rawPath") or "/"
    headers = event.get("headers") or {}
    body = event.get("body")
    if body is not None and event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return method, path, headers, body


def handler(event, context):
    method, path, headers, raw_body = _request(event)

    if method.upper() == "OPTIONS":
        return {"statusCode": 204, "headers": _CORS, "isBase64Encoded": False, "body": ""}

    # 1. x402 gate — reject before doing any DEM work.
    decision = metering.gate(method, path, headers)
    if decision.action == "reject":
        err = decision.error or {"status": 402, "code": "payment_required", "detail": ""}
        return _json_response(err["status"], {"error": err["code"], "detail": err["detail"]})

    # 2. Parse + render.
    try:
        boundary = json.loads(raw_body) if raw_body else None
        if not isinstance(boundary, dict):
            raise ValueError("request body must be a GeoJSON object")
    except (ValueError, TypeError) as exc:
        return _json_response(400, {"error": "bad_request", "detail": str(exc)})

    # Lazy import so gate/parse/billing unit tests don't have to load GDAL
    # (mirrors app.handler deferring the geoworker import).
    from app.sync_elevation import render_elevation

    try:
        result = render_elevation(boundary)
    except ValueError as exc:            # bad / oversized boundary
        return _json_response(422, {"error": "unprocessable", "detail": str(exc)})
    except Exception as exc:             # DEM read / off-coverage / GDAL
        logger.exception("elevation render failed")
        return _json_response(502, {"error": "render_failed", "detail": str(exc)})

    # 3. Bill the served call (best-effort; never fails the delivered response).
    if decision.action == "serve_and_record" and decision.record_key_hash:
        metering.report_usage(decision.record_key_hash, decision.endpoint_slug)

    return {
        "statusCode": 200,
        "headers": {
            "content-type": "image/png",
            "x-extent": json.dumps(result["extent"]),
            **_CORS,
        },
        "isBase64Encoded": True,
        "body": base64.b64encode(result["png"]).decode("ascii"),
    }
