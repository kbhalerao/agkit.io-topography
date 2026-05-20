"""
Signed-URL postback client.

Replaces USGS2021's `post_result` (which used Django `/api-token-auth/`
and shipped credentials in env). Every artifact carries its own signed,
time-bounded `post_url` minted by the Django service layer; the URL is
the entire authority. Lambda does not see Django credentials.

Two transports:

* `post_raster(post_url, filepath, parameter, layer, filename)`
  → `multipart/form-data` POST. Body has `file`, `parameter`, `layer`.
  Maps to ``tier2apps/topography/api_views.py:RasterPostbackView``.

* `post_scalar(scalar_url, parameter, value, units, lambda_function, source)`
  → JSON POST. Body has `{parameter, value, units, lambda_function, source}`.
  Maps to ``tier2apps/topography/api_views.py:ScalarPostbackView``.

Return value is the `requests.Response`; callers can `.ok`/`.status_code`
to decide retry behavior. We do not retry here — SQS will redeliver the
whole message on failure, which keeps the retry policy in one place.
"""
from __future__ import annotations

import json
import logging
from typing import Optional, Sequence

import requests

from app import settings

logger = logging.getLogger(__name__)


def post_raster(
    post_url: str,
    filepath: str,
    parameter: str,
    layer: str,
    filename: Optional[str] = None,
    extent: Optional[Sequence[float]] = None,
    request_signature: Optional[str] = None,
) -> requests.Response:
    """POST a raster artifact to its signed Django postback URL.

    `extent`, when given, is the artifact's true bounds as
    ``[west, south, east, north]`` in EPSG:4326. PNG carries no georef, so
    Django stores this on the layer and the frontend overlays the image at
    these exact bounds instead of stretching it to the field bbox.

    `request_signature`, when given, is the sha1 the Django service layer
    minted at request time (it identifies the bundle inputs). Echoed back
    verbatim so Django can stamp it on the resulting layer row — that's how
    the freshness check detects layers from a prior bundle version.
    """
    name = filename or filepath.rsplit("/", 1)[-1]
    with open(filepath, "rb") as fp:
        files = {"file": (name, fp)}
        data = {"parameter": parameter, "layer": layer or ""}
        if extent is not None:
            data["extent"] = json.dumps([float(v) for v in extent])
        if request_signature:
            data["request_signature"] = request_signature
        response = requests.post(
            post_url,
            files=files,
            data=data,
            timeout=settings.POSTBACK_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    if not response.ok:
        logger.warning(
            "raster postback non-2xx: url=%s status=%s body=%s",
            post_url, response.status_code, response.text[:200],
        )
    return response


def post_scalar(
    scalar_url: str,
    parameter: str,
    value: float,
    units: str = "",
    lambda_function: str = "",
    source: str = "usgs_10m",
) -> requests.Response:
    """POST a single scalar measurement to its signed Django postback URL."""
    payload = {
        "parameter": parameter,
        "value": float(value),
        "units": units,
        "lambda_function": lambda_function,
        "source": source,
    }
    response = requests.post(
        scalar_url,
        json=payload,
        timeout=settings.POSTBACK_TIMEOUT_SECONDS,
        allow_redirects=False,
    )
    if not response.ok:
        logger.warning(
            "scalar postback non-2xx: url=%s status=%s body=%s",
            scalar_url, response.status_code, response.text[:200],
        )
    return response
