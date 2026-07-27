# Migrating a topography consumer onto the x402-metered async endpoint

This guide is for any service that runs topography jobs through the SQS/Lambda
pipeline and wants to move onto the **metered** path — specifically **legacy
LabCore2026**, whose topo-async consumption predates x402. The reference
implementation is the AgKit mother package (`agkit.io-backend`); a LabCore dev
copies that pattern.

## What changes, and what doesn't

The topography Lambda, the job payload, and the postback contract are
**unchanged**. Migration swaps exactly one thing: **how the job is delivered.**

| | Before (unmetered) | After (x402-metered) |
|---|---|---|
| Delivery | AWS SDK → SQS/S3 (Lambda trigger) | `POST https://topo.agkit.io/elevation/async` |
| Credential | AWS IAM (SDK signing) | `Authorization: Bearer <x402 bypass token>` |
| Payload | the event JSON you already build | **the same event JSON**, verbatim |
| Result delivery | Lambda POSTs to your `post_url` | **unchanged** — Lambda POSTs to the same `post_url` |
| Billing | none | one usage event **per job, on a successful postback** |

The endpoint (`app/http_handler.py::_handle_async`) is a thin metered proxy: it
gates the request, stamps the resolved consumer onto each job, drops the payload
on the same `jobs` SQS queue the worker drains, and returns `202`. Your job's
signed `post_url` rides along untouched, so results land exactly where they do
today. **Record-on-completion:** the worker reports usage only after a job's
postback succeeds, so an accepted-then-failed job never bills.

## The reference implementation

`agkit.io-backend/tier2apps/topography/services.py::LambdaEventBuilder`. The
transport is chosen at publish time — set `TOPOGRAPHY_X402_ASYNC_URL` and it
POSTs; leave it unset and it falls back to direct SQS. The whole of the new path
is `_publish_http`:

```python
def _publish_http(self, payload, url: str) -> dict:
    token = getattr(settings, "TOPOGRAPHY_X402_BYPASS_TOKEN", "") or ""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.post(url, json=payload, headers=headers, timeout=10)
    return self.PUBLISH_OK if resp.status_code == 202 else self.PUBLISH_HTTP_FAIL
```

`payload` is exactly what `build_event()` produces — a JSON list, one item per
job, each carrying `metadata` (field boundary, `field_id`, ...) and a `post`
block with the signed `post_url`(s). Nothing about that changes; you already
build it.

### How erosion consumes it

`agkit.io-erosion` needs **no code change** — its `AOITopographyEventBuilder`
subclasses the mother's builder and inherits `_publish`. The cutover is two
deploy env vars (`deploy/terraform`):

```hcl
TOPOGRAPHY_X402_ASYNC_URL    = "https://topo.agkit.io/elevation/async"
TOPOGRAPHY_X402_BYPASS_TOKEN = "<bypass token for the erosion org>"
```

That is the whole cutover for a mother-based daughter. LabCore, which builds its
own event JSON, needs the equivalent one-function change below.

## LabCore2026 migration

### Today

LabCore delivers topo-async jobs via **S3 upload** (an S3-PutObject Lambda
trigger), not SQS:

- `common/lambda_utils.py::LambdaEventHandler` builds the event JSON
  (`lambda_job_json` → `metadata` + `post.output[].post_url`, the `post_url`
  built by `reverse('field_geo_raster_post', ...)`).
- `write_field_to_lambda_bucket(event, config['bucket'], key)` uploads it to the
  receiver bucket with `boto3` (AWS IAM creds).
- The Lambda POSTs results back to `field_geo_raster_post`
  (`clients/views/views_gis.py::FieldGeoRasterPost.post`), which persists the
  raster/`SubfieldLayer` and pings the UI over WebSocket.

### After

Replace **only** the upload step. `LambdaEventHandler.write_lambda_events` (the
method that calls `write_field_to_lambda_bucket`) becomes:

```python
import requests
from django.conf import settings

def write_lambda_events(self, PROCESS_CONFIG_KEY="public_elev_10m"):
    event = self.lambda_event_json(PROCESS_CONFIG_KEY)   # unchanged — same JSON
    url = getattr(settings, "TOPOGRAPHY_X402_ASYNC_URL", "")
    if not url:
        return self._write_to_s3_bucket(event, PROCESS_CONFIG_KEY)  # legacy fallback

    token = getattr(settings, "TOPOGRAPHY_X402_BYPASS_TOKEN", "")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.post(url, json=event, headers=headers, timeout=10)
    return resp.status_code == 202
```

Notes:

1. **Payload is unchanged.** LabCore's event uses `soildx_prefix`/`field_id` and
   a `reverse()`-built `post_url`; that's fine — the endpoint forwards the
   payload verbatim and the Lambda POSTs to whatever `post_url` it carries. You
   do **not** need to adopt the mother's `site_prefix`/magic-signed URLs to
   migrate. (You may later, but it's orthogonal.)
2. **The `field_geo_raster_post` receiver stays exactly as is.** Results still
   arrive there; the WebSocket ping still fires. No change to the inbound side.
3. **Keep the S3 path as a fallback** guarded by the setting, so the cutover is
   reversible and can go field-by-field / tenant-by-tenant.

### Getting a bypass token

1. In the x402 admin (`agkit.io-x402-backend`), create a `BypassToken` for the
   LabCore consumer, permitting the `elevation-async` CatalogItem (product group
   `topo`).
2. Put the token key in LabCore settings as `TOPOGRAPHY_X402_BYPASS_TOKEN` and
   the endpoint as `TOPOGRAPHY_X402_ASYNC_URL`
   (`https://topo.agkit.io/elevation/async`).
3. Until the token is set, `TOPOGRAPHY_X402_ASYNC_URL` empty ⇒ LabCore stays on
   the legacy S3 path — no behavior change.

### Enforcement posture

`elevation-async` seeds at **monitor** (record usage, reject nothing). A call
with no/invalid token is still served — you get identity + usage where a token
is present, and nothing breaks where it isn't. Flip the CatalogItem to
**enforce** only after every migrating consumer carries a valid token. See
`seed_topography_catalog.py --enforce`.

## Verifying the cutover

- A migrated call returns HTTP `202` with `{"status":"accepted","jobs":N,...}`.
- The raster/vector still lands on the field (postback unchanged).
- A `UsageRecord` for the consumer + `elevation-async` increments **after** the
  postback lands (not at accept) — check the x402 ledger dashboard.
- A job whose postback fails leaves usage un-incremented (record-on-completion).

## References

- Endpoint + worker: `app/http_handler.py`, `app/publisher.py`,
  `app/geoworker.py::process_payload` (this repo). See also `../CLAUDE.md`
  § "Async over HTTP (x402-metered)".
- Reference impl: `agkit.io-backend/tier2apps/topography/services.py::LambdaEventBuilder._publish_http`.
- Catalog row: `agkit.io-x402-backend/apps/catalog/management/commands/seed_topography_catalog.py` (`elevation-async`).
- LabCore today: `common/lambda_utils.py` (`LambdaEventHandler`), `clients/views/views_gis.py::FieldGeoRasterPost`.
