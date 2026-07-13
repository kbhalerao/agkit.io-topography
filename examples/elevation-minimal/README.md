# Sync elevation endpoint — boundary in, PNG bytes out

A synchronous slice of the topography engine: **POST a GeoJSON boundary, get a
colorized USGS 10 m elevation PNG back in the response.** No queue, no Lambda
postback, no GDAL on the caller's side. Intended as an **x402-metered HTTP
endpoint** billed at the compute rate.

```
app/sync_elevation.py                     # the core: render_elevation(geojson) -> {png, extent, ...}
app/http_handler.py                       # API Gateway handler (2nd trigger on the SQS image)
app/metering.py                           # x402 gate + usage report (Lambda-adapted prismuserv mimic)
tests/test_sync_elevation.py              # hermetic (fixture) + opt-in live render tests, run in the image
tests/test_http_handler.py                # gate + handler unit tests (pure, no GDAL)
examples/elevation-minimal/
├── service.py                            # FastAPI wrapper — a local reference for the same HTTP shape
├── requirements.txt
└── README.md                             # this file
../agkit.io-x402-backend/
└── apps/catalog/management/commands/seed_topography_catalog.py   # the x402 catalog wiring
```

**Architecture (decided):** API Gateway HTTP API → a **second Lambda function on
the same container image** (`handler = app.http_handler.handler`), Cloudflare
CNAME `topo.agkit.io`, us-west-2. Cold starts (~2–5 s) are acceptable. The
**Lambda self-bills** — no gateway in front does it — by POSTing to the x402
backend, mimicking prismuserv's metering (`app/metering.py`). GDAL stays in our
image; it is never added to `agkit.io-services`.

---

## Why this can be synchronous

The full topography engine is async (SQS → Lambda → signed postback) **because
of the flow jobs** — GRASS `r.watershed`, MFD flow accumulation, contours over a
buffered DEM. Those are slow and memory-heavy.

Elevation coloring carries none of that weight — three cheap steps:

1. bbox of the boundary,
2. read only the overlapping blocks of the USGS DEM tile over `/vsis3/` HTTP
   range reads (the tiles are valid COGs, so a field-scale window is a few
   hundred KB, not the ~440 MB whole tile),
3. `gdaldem color-relief` → PNG.

Field / HUC-12 scale in-region: sub-second to ~2 s — inside a sync budget.

### Three things that make or break it (all in `app/sync_elevation.py`)

1. **Run it in `us-west-2`**, co-located with `prd-tnm`, or every range read
   crosses the internet and you pay egress.
2. **Read the COG unsigned** (`AWS_NO_SIGN_REQUEST=YES`) — prd-tnm allows
   anonymous reads and the role has no grant; a *signed* read 403s.
3. **Cap the input area** — `MAX_TILES=4` rejects a whole-state polygon before
   it blows the latency/memory budget.

---

## Test it inside the Lambda image

The host has no GDAL, so the render tests run inside the image (memory
`lambda-image-runtime`). `IN_TEST=true` makes the tile reader use local fixture
tiles, so the default suite is offline:

```bash
./build_deployment.sh
docker run --rm -v "$PWD:/code" agkit-topography:latest \
    python -m unittest tests.test_sync_elevation -v
```

- **Hermetic** — tile-naming asserts + a full render against
  `tests/USGS_13_n42w092.tif` (fixture tile, no network).
- **Live (opt-in)** — set `LIVE_DEM=1` to hit real prd-tnm for an Oregon
  boundary and validate the actual unsigned `/vsis3/` COG read:

  ```bash
  docker run --rm -e LIVE_DEM=1 agkit-topography:latest \
      python -m unittest tests.test_sync_elevation -v
  ```

### As a service

```bash
# inside the image / any GDAL env, in us-west-2:
uvicorn examples.elevation-minimal.service:app --port 8040

curl -sS -X POST http://localhost:8040/x402/v1/topo/elevation \
     -H 'content-type: application/json' \
     --data '{"type":"Polygon","coordinates":[[[-123.30,44.55],
              [-123.29,44.55],[-123.29,44.56],[-123.30,44.56],
              [-123.30,44.55]]]}' \
     -o field.png -D -
```

Body = raw PNG. Bounds come back in **`X-Extent`** as
`[west, south, east, north]` (a color-relief PNG has no georeferencing), which
a MapLibre / Leaflet image overlay needs to place it.

---

## Billing it through x402 — the two questions answered

**Is the Lambda wired to bill behind x402 today? No.** The two are separate
paths: x402 is a payment/metering control plane whose catalog is seeded from
origin services (`agkit.io-services`), and origins meter *themselves* against it
(`agkit.io-services/app/metering/`). The topography Lambda is triggered by SQS
from the main backend and never sees a 402. So the sync HTTP Lambda **meters
itself** (`app/metering.py`) — a Lambda-adapted port of prismuserv's metering:
it pulls the catalog config from `GET /api/v1/adapter/config/`, gates the request
against it, and POSTs the served call to `POST /api/v1/adapter/usage/`. Two
changes from prismuserv for the Lambda runtime: config is cached in a module
global with a TTL (no daemon poller survives a freeze), and usage is reported
synchronously per request (no queue/flush thread). Rate limiting drops out —
throttle at API Gateway.

**Does building this out spare the colleague the GDAL install? Yes — that's the
point.** GDAL/GRASS live in our image; the caller POSTs a boundary and gets
bytes. Zero geospatial deps on their side.

### Catalog wiring — both modes at the compute tier

`seed_topography_catalog.py` (in the x402 repo, modeled on `seed_agwx_catalog`)
creates a `topo` ProductGroup and the elevation product in **both** delivery
modes, both on the **compute** tier ($0.010 / call):

| item              | mode  | path                        | price                      |
|-------------------|-------|-----------------------------|----------------------------|
| `elevation`       | SYNC  | `/x402/v1/topo/elevation`       | `$0` → charges compute tier |
| `elevation-async` | ASYNC | `/x402/v1/topo/elevation/async` | `$0.010` (deposit `$0.005`) |

The SYNC item uses `price=$0` so it charges the compute **tier** rate — a
rate-card change propagates without touching the catalog. The ASYNC item can't
do that: `CatalogItem.clean()` requires `deposit < price`, so `$0` leaves no room
for a deposit. It therefore pins the explicit compute rate (`$0.010`) with a
half-deposit. Both end up at compute.

```bash
# in agkit.io-x402-backend/
uv run python manage.py seed_topography_catalog --dry-run   # preview
uv run python manage.py seed_topography_catalog             # monitor mode
uv run python manage.py seed_topography_catalog --enforce   # bill it
```

Items default to `enforcement=monitor` (record usage, reject nothing); flip to
`--enforce` when ready. The Lambda reads each item's enforcement from the config
it pulls, so no redeploy is needed to switch. Consumer side (bypass tokens) —
mint per-consumer keys following
`apps/tenants/management/commands/setup_erosion_consumer.py`; the raw key is what
the caller sends as `Authorization: Bearer <key>`.

The other topography functions (slope, watershed, contours, flowlines) slot in
as further ASYNC compute items following the same row shape.

---

## Deploying it

Infra is in `deploy/sync_http.tf` — a second Lambda from the same image
(`command = ["app.http_handler.handler"]`) + an API Gateway HTTP API
(`POST /elevation`) + an optional custom domain. `deploy.sh` reads the
sync-endpoint config from the environment (all optional; unset → terraform
default). Do it in this order.

### Phase 1 — ship the endpoint, unmetered, on the execute-api URL

Metering off, no domain. Proves the render path end to end.

```bash
cd deploy
./deploy.sh deploy                       # builds+pushes image, applies BOTH functions
API=$(terraform output -raw sync_http_api_endpoint)
curl -sS -X POST "$API/elevation" -H 'content-type: application/json' \
     --data '{"type":"Polygon","coordinates":[[[-123.30,44.55],[-123.29,44.55],
              [-123.29,44.56],[-123.30,44.56],[-123.30,44.55]]]}' -o field.png -D -
```

### Phase 2 — x402 provisioning (in `agkit.io-x402-backend/`)

```bash
uv run python manage.py create_gateway_user --username topo-gateway   # -> gateway token
uv run python manage.py seed_topography_catalog                        # monitor mode
# mint consumer bypass tokens (setup_erosion_consumer pattern) -> the Bearer keys
```

### Phase 3 — turn metering on (still monitor mode; nothing is rejected yet)

```bash
export METERING_ENABLED=true
export X402_BASE_URL=https://x402.agkit.io
export X402_GATEWAY_TOKEN=…              # from phase 2 (kept out of the CLI args)
./deploy.sh deploy                       # redeploys the sync fn with metering env
```

Call it with `Authorization: Bearer <consumer-key>` and confirm usage counters
increment in x402. Flip `seed_topography_catalog --enforce` when ready to reject
unpaid calls — no Lambda redeploy needed (enforcement is read from the pulled
config).

### Phase 4 — custom domain (topo.agkit.io)

1. **Request the ACM cert** (must be in the API's region, us-west-2):
   ```bash
   CERT=$(aws acm request-certificate --region us-west-2 \
       --domain-name topo.agkit.io --validation-method DNS \
       --query CertificateArn --output text)
   aws acm describe-certificate --region us-west-2 --certificate-arn "$CERT" \
       --query 'Certificate.DomainValidationOptions[0].ResourceRecord'
   ```
2. **Add the validation CNAME in Cloudflare** (agkit.io zone → DNS): Name = the
   returned `Name` **without** the `.agkit.io` suffix (Cloudflare re-appends the
   zone), Target = the returned `Value`, **Proxy: DNS only (grey cloud)**. Then
   `aws acm wait certificate-validated --region us-west-2 --certificate-arn "$CERT"`.
3. **Apply the domain:**
   ```bash
   export CUSTOM_DOMAIN_NAME=topo.agkit.io
   export CERTIFICATE_ARN="$CERT"
   ./deploy.sh deploy
   TARGET=$(terraform output -raw sync_http_custom_domain_target)
   ```
4. **Point the hostname in Cloudflare:** `CNAME topo` → `$TARGET`, **DNS only**
   to start (TLS terminates at API Gateway on the ACM cert). Switch to proxied
   later only with SSL mode Full (strict).

A gitignored `deploy/terraform.tfvars` is auto-loaded and overrides these env
vars if you prefer a file.

---

## What was deliberately left out

GRASS/flow, blended hillshade, projected-CRS resampling, scalar/vector outputs,
S3 write — none are needed for bytes-out-over-HTTP. Source of truth for the full
pipeline: `app/geoworker.py` (`elev_public_10m`), `app/color_schemes.py`,
`app/geocolorize.py`.
