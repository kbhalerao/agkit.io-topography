"""
SQS-triggered Lambda entrypoint.

Replaces USGS2021's S3 PutObject trigger. The Django side now publishes
each topography request as a single SQS message whose body is the JSON
array produced by `tier2apps/topography/services.py:LambdaEventBuilder.
build_event()` — one item per job, each with its own signed `post_url`.

The handler:

1. Iterates `event['Records']`.
2. For each record, parses `record['body']` as JSON and dispatches
   to ``geoworker.process_payload``.
3. On a per-record exception, appends the record's `messageId` to
   ``batchItemFailures`` so SQS retries *only* the failed messages
   (partial-batch response). See:
   https://docs.aws.amazon.com/lambda/latest/dg/with-sqs.html#services-sqs-batchfailurereporting

The Lambda's event-source mapping MUST have
``FunctionResponseTypes=["ReportBatchItemFailures"]`` for the partial-
batch contract to take effect. With the default mapping, any
exception would fail the entire batch.
"""
from __future__ import annotations

import json
import subprocess

from app import settings


def _cleanup_tmp() -> None:
    """Best-effort `/tmp` wipe between invocations. Lambda reuses warm
    containers — leftover GRASS locations or tile caches can OOM the
    512 MB ephemeral disk. Skipped in IN_TEST mode so unit tests don't
    paw at the host's /tmp."""
    if settings.IN_TEST:
        return
    try:
        subprocess.call(
            "rm -rf /tmp/*", shell=True,
            stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _parse_body(body) -> list:
    """
    Accept the SQS body in two shapes:

    * The list of job dicts directly (the canonical shape).
    * A single job dict (we wrap in a one-element list for convenience).

    Anything else is rejected so the message gets parked on the DLQ
    instead of looping forever.
    """
    if isinstance(body, str):
        body = json.loads(body)
    if isinstance(body, dict):
        body = [body]
    if not isinstance(body, list):
        raise ValueError(f"SQS body must be list or dict, got {type(body).__name__}")
    return body


def handler(event, context):
    """
    Lambda entrypoint. `event` is the SQS event envelope; `context` is
    the standard Lambda context (unused).

    Returns a dict with `batchItemFailures` per the partial-batch
    contract. An empty list means the whole batch succeeded.
    """
    # Lazy import so unit tests of the dispatch logic don't have to load
    # GDAL / GRASS via the geoworker import chain.
    from app import geoworker

    records = event.get("Records") or []
    failures: list[dict] = []
    results = []

    for record in records:
        message_id = record.get("messageId", "?")
        try:
            payload = _parse_body(record["body"])
            result = geoworker.process_payload(payload)
            results.append({"messageId": message_id, "result": result})
        except Exception as exc:
            print(f"handler: record {message_id} failed: {exc!r}")
            failures.append({"itemIdentifier": message_id})

    _cleanup_tmp()
    print({"results": results, "failures": failures})
    return {"batchItemFailures": failures}
