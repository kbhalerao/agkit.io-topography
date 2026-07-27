"""SQS publisher for the HTTP async endpoint.

The async elevation front door (``app.http_handler``) is a metered SQS-publish
proxy: it gates the request, then drops the (unchanged) job payload onto the
same ``jobs`` queue the SQS worker (``app.handler``) drains. The caller's own
signed ``post_url`` rides along inside the payload, so the postback path is
identical to a job Django published directly — the only new thing is that the
call is now metered.

Kept out of ``app.handler``/``app.geoworker`` so the accept path never imports
GDAL/GRASS. boto3 reads AWS creds from the Lambda execution role at runtime
(the role gains ``sqs:SendMessage`` on the jobs queue); no static keys.
"""
from __future__ import annotations

import json
import logging

from app import settings

logger = logging.getLogger(__name__)


def publish_jobs(body: list | dict) -> str | None:
    """Send the job payload to the SQS jobs queue as one message.

    Returns the SQS ``MessageId`` on success. Returns ``None`` in IN_TEST mode
    (no network hop for unit tests). Raises on a real send failure so the
    handler can answer 502 — unlike usage metering, a lost job is not
    best-effort: if we cannot enqueue it, the caller must know it was not
    accepted.
    """
    if settings.IN_TEST:
        return None
    if not settings.JOBS_QUEUE_URL:
        raise RuntimeError("JOBS_QUEUE_URL is not configured")

    import boto3  # lazy: keep boto3 off the gate/parse import path

    client = boto3.client("sqs", region_name=settings.AWS_REGION)
    resp = client.send_message(
        QueueUrl=settings.JOBS_QUEUE_URL,
        MessageBody=json.dumps(body),
    )
    message_id = resp.get("MessageId")
    logger.info("async job published to SQS: message_id=%s", message_id)
    return message_id
