"""x402 metering & gating for the topography Lambda.

Lambda-adapted port of prismuserv's `metering/` (middleware + config + usage +
matcher). The decision table and config/consumer shapes are mirrored verbatim;
two things change for the Lambda execution model:

* **Config is fetched synchronously and cached in a module global with a TTL**
  — no daemon-thread poller, because a frozen Lambda can't run one. A cold
  start (or a cache older than METERING_CONFIG_REFRESH_SECONDS) triggers a
  best-effort refresh; on failure the last good config is served, and an
  empty cache means every path is unmetered (fail-open, same as prismuserv).

* **Usage is reported synchronously per request** — no in-memory queue + flush
  thread, which wouldn't drain before the Lambda freezes. The DEM render
  dominates latency, so a ~100 ms usage POST is negligible.

Rate limiting is intentionally dropped: an in-memory token bucket is
meaningless across Lambda concurrency. Throttle at API Gateway instead.

Decision table (ported from prismuserv MeteringMiddleware._decide):
  1. enabled=false                 -> serve (unmetered)
  2. method=OPTIONS                -> serve (CORS preflight)
  3. no catalog entry for path     -> serve (unmetered)
  4. enforcement=monitor           -> serve; record if a known active consumer
  5. enforce + no/invalid cred     -> 401
  6. enforce + cred unpermitted    -> 403
  7. enforce + valid cred          -> serve; record
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)


# --- settings (env-driven; names mirror prismuserv/metering/settings.py) ----

def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


ENABLED = _env_bool("METERING_ENABLED", False)
X402_BASE_URL = os.environ.get("X402_BASE_URL", "http://localhost:8402")
X402_GATEWAY_TOKEN = os.environ.get("X402_GATEWAY_TOKEN", "")
CATALOG_PATH_PREFIX = os.environ.get("METERING_CATALOG_PATH_PREFIX", "/x402/v1")
AUTH_HEADER = os.environ.get("METERING_AUTH_HEADER", "Authorization").lower()
AUTH_SCHEME = os.environ.get("METERING_AUTH_SCHEME", "Bearer").strip()
CONFIG_REFRESH_SECONDS = _env_int("METERING_CONFIG_REFRESH_SECONDS", 60)
HTTP_TIMEOUT = float(os.environ.get("METERING_HTTP_TIMEOUT", "10"))


# --- catalog matcher (ported from prismuserv/metering/matcher.py) -----------

@dataclass(frozen=True)
class CatalogEndpoint:
    slug: str
    method: str
    path: str          # canonical catalog path, e.g. /x402/v1/topo/elevation
    enforcement: str    # "monitor" | "enforce"
    enabled: bool = True


_BRACE_PARAM = re.compile(r"\{([^/}]+)\}")


def _compile(path: str) -> re.Pattern[str]:
    pattern = re.escape(path).replace(r"\{", "{").replace(r"\}", "}")
    return re.compile(f"^{_BRACE_PARAM.sub(r'[^/]+', pattern)}$")


@dataclass
class ConsumerRecord:
    key_hash: str
    consumer_name: str
    is_active: bool
    permitted_endpoints: frozenset


@dataclass
class CachedConfig:
    routes_by_method: dict
    consumers_by_hash: dict
    config_version: int
    fetched_at: float


def _endpoints_from_blob(blob: dict) -> list[CatalogEndpoint]:
    out = []
    for group in blob.get("product_groups") or []:
        if not group.get("enabled", True):
            continue
        for ep in group.get("endpoints") or []:
            out.append(CatalogEndpoint(
                slug=ep["id"],
                method=ep["method"].upper(),
                path=ep["path"],
                enforcement=ep.get("enforcement", "monitor"),
                enabled=ep.get("enabled", True),
            ))
    return out


def _build_cache(blob: dict) -> CachedConfig:
    routes_by_method: dict = {}
    for ep in _endpoints_from_blob(blob):
        if ep.enabled:
            routes_by_method.setdefault(ep.method, []).append((_compile(ep.path), ep))
    consumers = {}
    for raw in blob.get("consumers") or []:
        kh = raw.get("key_hash")
        if kh:
            consumers[kh] = ConsumerRecord(
                key_hash=kh,
                consumer_name=raw.get("consumer_name", ""),
                is_active=bool(raw.get("is_active", False)),
                permitted_endpoints=frozenset(raw.get("permitted_endpoints") or ()),
            )
    return CachedConfig(
        routes_by_method=routes_by_method,
        consumers_by_hash=consumers,
        config_version=int(blob.get("config_version") or 0),
        fetched_at=time.time(),
    )


# Module-global cache, persists across warm Lambda invocations.
_cache: CachedConfig | None = None


def _get_config() -> CachedConfig | None:
    """Return the cached config, refreshing synchronously if stale/cold.

    Best-effort: on fetch failure the last good cache is kept; if there has
    never been a successful fetch, returns None and every path is unmetered.
    """
    global _cache
    if not X402_GATEWAY_TOKEN:
        return _cache
    fresh = _cache is not None and (time.time() - _cache.fetched_at) < CONFIG_REFRESH_SECONDS
    if fresh:
        return _cache
    try:
        resp = requests.get(
            f"{X402_BASE_URL.rstrip('/')}/api/v1/adapter/config/",
            headers={"Authorization": f"Token {X402_GATEWAY_TOKEN}",
                     "Accept": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 200:
            _cache = _build_cache(resp.json())
        else:
            logger.warning("metering: config fetch got %s; keeping cache", resp.status_code)
    except Exception as exc:  # noqa: BLE001 — never fail the request on metering
        logger.warning("metering: config fetch failed (%s); keeping cache", exc)
    return _cache


def _resolve(cfg: CachedConfig, method: str, path: str) -> CatalogEndpoint | None:
    candidate = f"{CATALOG_PATH_PREFIX.rstrip('/')}{path}"
    for regex, ep in cfg.routes_by_method.get(method.upper(), ()):
        if regex.match(candidate):
            return ep
    return None


# --- credential + decision --------------------------------------------------

def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _extract_credential(headers: dict) -> str | None:
    # API Gateway v2 lower-cases header names; look up the lower-cased header.
    header = (headers or {}).get(AUTH_HEADER)
    if not header:
        return None
    if AUTH_SCHEME:
        prefix = f"{AUTH_SCHEME} "
        return header[len(prefix):].strip() or None if header.startswith(prefix) else None
    return header.strip() or None


@dataclass
class Decision:
    action: str                 # "serve" | "serve_and_record" | "reject"
    endpoint_slug: str | None = None
    record_key_hash: str | None = None   # non-None => bill this consumer on 2xx
    error: dict | None = field(default=None)   # {"status","code","detail"} on reject


def _err(status: int, code: str, detail: str) -> Decision:
    return Decision(action="reject", error={"status": status, "code": code, "detail": detail})


def gate(method: str, path: str, headers: dict) -> Decision:
    """Mirror of prismuserv MeteringMiddleware._decide, Lambda-adapted."""
    if not ENABLED:
        return Decision("serve")
    if method.upper() == "OPTIONS":
        return Decision("serve")

    cfg = _get_config()
    endpoint = _resolve(cfg, method, path) if cfg else None
    if endpoint is None:
        return Decision("serve")  # unmetered

    raw_key = _extract_credential(headers)
    key_hash = hash_key(raw_key) if raw_key else None
    consumer = cfg.consumers_by_hash.get(key_hash) if (cfg and key_hash) else None

    if endpoint.enforcement.lower() == "monitor":
        record = key_hash if (consumer and consumer.is_active) else None
        return Decision("serve_and_record", endpoint.slug, record)

    # enforce
    if consumer is None:
        return _err(401, "unidentified" if raw_key is None else "invalid",
                    "Consumer credential required.")
    if not consumer.is_active:
        return _err(401, "invalid", "Consumer credential is inactive.")
    if endpoint.slug not in consumer.permitted_endpoints:
        return _err(403, "unpermitted",
                    f"Consumer not permitted for endpoint {endpoint.slug}.")
    return Decision("serve_and_record", endpoint.slug, key_hash)


# --- usage report (synchronous; prismuserv batches, we can't across freezes) -

def report_usage(key_hash: str, endpoint_slug: str, count: int = 1) -> bool:
    """POST one usage event to the x402 usage-ingest endpoint. Best-effort;
    never raises — billing must not fail a delivered request."""
    if not X402_GATEWAY_TOKEN:
        return False
    payload = {"events": [{
        "key_hash": key_hash,
        "endpoint": endpoint_slug,
        "count": count,
        "year_month": datetime.now(timezone.utc).strftime("%Y-%m"),
    }]}
    try:
        resp = requests.post(
            f"{X402_BASE_URL.rstrip('/')}/api/v1/adapter/usage/",
            json=payload,
            headers={"Authorization": f"Token {X402_GATEWAY_TOKEN}",
                     "Content-Type": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("metering: usage report failed (%s)", exc)
        return False
    if not (200 <= resp.status_code < 300):
        logger.warning("metering: usage report got %s; body=%r",
                       resp.status_code, resp.text[:300])
        return False
    return True


def install_config_for_tests(blob: dict) -> None:
    """Bypass HTTP and install a cache directly (test helper, mirrors prismuserv)."""
    global _cache
    _cache = _build_cache(blob)
