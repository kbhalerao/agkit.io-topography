"""API Gateway HTTP handler for the elevation endpoints (sync + async).

Entrypoint: `app.http_handler.handler`. This is the SECOND trigger on the same
container image as the SQS worker (`app.handler.handler`) — the image already
carries GDAL, so an HTTP surface is just another handler. Deploy it as its own
Lambda function (own memory/timeout/concurrency), fronted by API Gateway HTTP
API with a Cloudflare CNAME (topo.agkit.io), in us-west-2.

Two routes, both x402-gated (app.metering):

  POST /elevation        SYNC  — boundary GeoJSON in; a JSON envelope carrying
                                 the color-relief PNG + single-band GeoTIFF out.
                                 Billed here: one usage event on a 2xx.

  POST /elevation/async  ASYNC — a metered SQS-publish proxy. The caller sends
                                 the job payload (the same shape Django's
                                 LambdaEventBuilder produces, carrying its own
                                 signed post_url); the handler gates it, stamps
                                 the resolved consumer onto each job, and drops
                                 it on the jobs queue the SQS worker drains.
                                 Billed on COMPLETION: the worker reports one
                                 usage event per job only after its postback
                                 succeeds (see app.geoworker.process_payload),
                                 so accepted-then-failed jobs never bill.

The Lambda self-bills (no gateway in front doing it): the gate resolves the
consumer from the cached x402 catalog config. See app.metering for the
config/usage contracts and app.publisher for the SQS publish.
"""
from __future__ import annotations

import base64
import json
import logging

from app import metering, publisher

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


def _handle_async(raw_body: str | None, decision) -> dict:
    """Accept an async topography job, meter-tag it, and enqueue it.

    The body is the job payload Django's LambdaEventBuilder already produces —
    a list of per-job dicts (or a single dict), each carrying its own signed
    `post_url`. We forward it verbatim onto the SQS jobs queue; the only
    mutation is stamping the resolved consumer onto each job under `metering`
    so the worker can bill on a successful postback. A job with no `metering`
    block (anonymous / unmetered) is processed but never billed.
    """
    try:
        jobs = json.loads(raw_body) if raw_body else None
    except (ValueError, TypeError) as exc:
        return _json_response(400, {"error": "bad_request", "detail": str(exc)})

    if isinstance(jobs, dict):
        jobs = [jobs]
    if not isinstance(jobs, list) or not jobs:
        return _json_response(
            400, {"error": "bad_request", "detail": "body must be a non-empty job list"})
    if not all(isinstance(j, dict) and "metadata" in j for j in jobs):
        return _json_response(
            400, {"error": "bad_request", "detail": "each job needs a metadata object"})

    # Stamp the consumer so the worker bills on completion (record-on-completion:
    # accepted-then-failed jobs never bill). Only when the gate resolved a
    # billable consumer for this call.
    if decision.action == "serve_and_record" and decision.record_key_hash:
        for job in jobs:
            job["metering"] = {
                "key_hash": decision.record_key_hash,
                "endpoint": decision.endpoint_slug,
            }

    try:
        message_id = publisher.publish_jobs(jobs)
    except Exception as exc:  # enqueue failed — the job was NOT accepted
        logger.exception("async enqueue failed")
        return _json_response(502, {"error": "enqueue_failed", "detail": str(exc)})

    return _json_response(202, {"status": "accepted", "jobs": len(jobs), "message_id": message_id})


def handler(event, context):
    method, path, headers, raw_body = _request(event)

    if method.upper() == "OPTIONS":
        return {"statusCode": 204, "headers": _CORS, "isBase64Encoded": False, "body": ""}

    # Warm-up endpoint — unmetered, no gate. Hitting it once boots the container
    # and primes the /vsis3 path so subsequent /elevation calls run warm-fast.
    if path == "/prime":
        from app.sync_elevation import prime
        try:
            return _json_response(200, prime())
        except Exception as exc:
            logger.exception("prime failed")
            return _json_response(502, {"error": "prime_failed", "detail": str(exc)})

    # 1. x402 gate — reject before doing any work. The gate is path-aware, so it
    # resolves /elevation vs /elevation/async to their own catalog items.
    decision = metering.gate(method, path, headers)
    if decision.action == "reject":
        err = decision.error or {"status": 402, "code": "payment_required", "detail": ""}
        return _json_response(err["status"], {"error": err["code"], "detail": err["detail"]})

    # Async route: hand the (gated) job off to SQS; billing happens on the
    # worker side when the postback lands. Anything else is the sync render.
    if path.rstrip("/") == "/elevation/async":
        return _handle_async(raw_body, decision)

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

    # Both artifacts + metadata ride in one JSON body: the PNG for display and
    # the single-band GeoTIFF for the actual elevation data. Binaries are
    # base64 (JSON can't carry raw bytes); `extent` places the georef-less PNG.
    return _json_response(200, {
        "png": base64.b64encode(result["png"]).decode("ascii"),
        "tif": base64.b64encode(result["tif"]).decode("ascii"),
        "extent": result["extent"],
        "width": result["width"],
        "height": result["height"],
    })
