# CLAUDE.md — agkit.io-topography

Dockerized Lambda for USGS 10m DEM-derived topography, plugged into
`agkit.io-backend/tier2apps/topography`. From-scratch rewrite of
`SoilDiagnostics/code/USGS2021`.

## Differences from USGS2021

| Aspect          | USGS2021                                 | agkit.io-topography                                       |
|-----------------|------------------------------------------|-----------------------------------------------------------|
| Trigger         | S3 PutObject on receiver bucket          | SQS queue with inline-JSON message body                   |
| Postback auth   | Django `/api-token-auth/` + username/pw  | Magic-signed `post_url` minted by Django (URL = authority)|
| L / LS rasters  | Computed by `r.watershed`, **not** exported | Computed **and** exported + scalar-reduced              |
| Settings        | `app/lambda_settings.py` w/ Django creds | `app/settings.py` — no Django creds; SQS region only      |

## Layout

```
app/
├── handler.py        # SQS event router (entrypoint: app.handler.handler)
├── geoworker.py      # LambdaGISProcessor — named function-per-parameter
├── grass_handler.py  # GRASS r.watershed; exports drainage/stream/spi/tci/L/LS
├── downloader.py     # USGS DEM tile fetch from prd-tnm (us-west-2)
├── ziphandler.py     # DEM tile clip-to-extent helper
├── geocolorize.py    # PNG colorization via gdaldem color-relief + blended_topo()
├── color_schemes.py  # Color ramps (elev / slope / drainage / tci / L / LS)
├── poster.py         # Signed-URL postback client (raster + scalar)
├── reducers.py       # Raster → scalar reduction over field polygon
└── settings.py       # Minimal env-driven config
```

## Event payload contract

The SQS message body is the JSON list produced by Django's
`tier2apps/topography/services.py:LambdaEventBuilder.build_event()`.
One outer list, one item per job. Per-item shape:

```jsonc
{
  "metadata": {
    "function_name": "watershed_length_slope_raster",
    "field_boundary": {"type": "FeatureCollection", "features": [...]},
    "watershed_boundary": {"type": "FeatureCollection", "features": [...]},
    "field_id": 123,
    "site_prefix": "agkit",
    "input_data": null
  },
  "post": {
    "domain": "https://api.example.com",
    "parameter": "watershed_length_slope_raster",
    "layer": "USGS Length Slope",
    "output": [
      {
        "filename": "watershed_length_slope_raster.tif",
        "extension": "tif",
        "post_url": "https://api.example.com/api/v1/topography/postback/raster/123/<token>/",
        "parameter": "watershed_length_slope_raster",
        "layer": "USGS Length Slope"
      }
    ]
    // Scalar jobs use "scalar_url" instead of "output":
    // "scalar_url": "https://api.example.com/api/v1/topography/postback/scalar/123/<token>/"
  }
}
```

Vector jobs (`contour_lines`, `mfd_flowlines`) use the same `output` shape
with `"extension": "geojson"`; the file extension is the routing signal on
the Django side.

`watershed_boundary` is **optional** — the user-selected analysis area
that bounds the flow-computation domain (a HUC-12, a USGS grid cell, or a
hand-drawn polygon; a FeatureCollection in EPSG:4326, one feature per
selected polygon). Flow jobs (`watershed_*`, `mfd_flowlines`) size their
DEM to its union bbox — `clipped_raster` for the field + ~220 m buffer,
`watershed_raster` for the flow domain. `null` or absent falls back to the
field-buffered domain; other jobs ignore it entirely. See
`docs/watershed-bounded-flow-feature.md`.

## Postback contract

What the Lambda POSTs back to the signed URLs:

- **Raster** (`postback/raster/...`): multipart form — `file` (PNG, plus a
  sidecar GeoTIFF for data layers), `parameter`, `layer`, and `extent`.
  `extent` is the artifact's true bounds, JSON-encoded as
  `[west, south, east, north]` in EPSG:4326. PNG carries no georeferencing,
  so the bounds travel with the file; Django stores them on the layer.
- **Vector**: a `.geojson` file POSTed to the *same* `postback/raster/...`
  endpoint. The `.geojson` extension routes it to the vector-layer upsert.
- **Scalar** (`postback/scalar/...`): JSON — `parameter`, `value`, `units`.

## Adding a new geo function

1. Add a method to `LambdaGISProcessor` in `app/geoworker.py`. Method
   signature is `def function_name(self, payload):`.
2. Use `self.build_common_cache(payload)` to access shared field
   shapefile, DEM raster, and clipped DEM raster.
3. Post the result, depending on the job kind:
   - **Raster (data layer):** return `self.handle_posting(file, color_map,
     payload)` — it colorizes the tif and posts PNG + tif with `extent`.
   - **Raster (pre-built PNG):** return `self._post_built_raster(png,
     payload, extent=...)` — no colorize step (e.g. blended topo).
   - **Vector:** produce a `.geojson` file and post it with
     `Poster.post_raster(...)`; the extension routes it server-side.
   - **Scalar:** compute the scalar and call `Poster.post_scalar(...)`.
4. Add a color ramp to `app/color_schemes.py` if needed.
5. On the Django side, add the parameter constant to
   `tier2apps/topography/schema.py` and the job to
   `settings.AWS_LAMBDA_EVENT_CONFIGURATION`.

## Operational notes

### SQS retry semantics — per-job errors are NOT retried

`handler.py` implements the partial-batch contract (`batchItemFailures`),
but in practice **a failed job never reaches the handler as an
exception**. `geoworker.process_payload` catches per-job errors and
returns an `"... resulted in ERROR."` string instead of raising, so the
handler sees the record as processed and returns `failures: []`. The
SQS message is consumed and never redriven.

Consequence: a *transient* failure (e.g. a postback that times out, a
DEM tile briefly unavailable) is lost — no SQS retry, no DLQ. Only a
crash *outside* the per-job try/except (bad message body, OOM, GRASS
init failure) produces a real `batchItemFailure` and gets retried.

If a daughter project needs transient-failure retries, the job result
must propagate back to the handler as a raised exception (or the
handler must inspect result strings and append to `batchItemFailures`).

### USGS DEM access — unsigned S3 client

DEM tiles come from the public `prd-tnm` Open Data bucket. The Lambda
execution role has **no IAM grant** on `prd-tnm` (by design — no static
keys, minimal role). A *signed* `get_object` therefore fails with
`AccessDenied`. `downloader.usgs_s3_client()` uses
`signature_version=UNSIGNED` for anonymous reads, which need no IAM
grant at all. The signed `s3_client()` is reserved for our own buckets
(sidecar-zip fetches). Any daughter project reading `prd-tnm` must do
the same — do not sign with the execution role.

## Cross-project references

- Backend topography app: `agkit.io-backend/tier2apps/topography/`
- Backend service that builds the event JSON: `services.py:LambdaEventBuilder`
- Backend postback views: `api_views.py:RasterPostbackView` / `ScalarPostbackView`
- Backend magic-link signing: `tier1apps/foundations/magic_signing.py`
