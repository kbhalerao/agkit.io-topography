# agkit.io-topography

Dockerized AWS Lambda for USGS 10m DEM-derived topography processing, plugged
into `agkit.io-backend/tier2apps/topography`.

This is a from-scratch rewrite of `SoilDiagnostics/code/USGS2021`. Functionality
and code structure are conserved; three new properties differentiate it:

1. **L and LS rasters** — `length_slope` and `slope_steepness` from
   GRASS `r.watershed` are now exported as raster artifacts (previously
   only `drainage`, `stream`, `spi`, `tci` were posted back).
2. **Zero-conf postback** — no Django credentials in the Lambda. Each
   artifact's destination `post_url` is a magic-signed, time-bounded URL
   minted by the Django side; the URL is the entire authority.
3. **SQS-triggered** — the Lambda consumes inline JSON event payloads
   from an SQS queue (replaces the S3 PutObject trigger).

## Pipeline overview

```
[Django: agkit.io-backend]                            [Lambda: this repo]
                                                                
  request_topography(field)                                     
     │                                                         
     │  build_event(): one job per parameter,                  
     │     each with its own signed post_url                   
     │                                                         
     ▼                                                          
  SQS:topography-jobs ─────────────────────────────▶  handler.handler
                                                       │
                                                       │  per Record:
                                                       │    geoworker.process_event(payload)
                                                       │    └ download USGS DEM tiles (prd-tnm)
                                                       │    └ clip + transform + (GRASS r.watershed)
                                                       │    └ per output: poster.post_raster(post_url, …)
                                                       │    └ scalars: poster.post_scalar(scalar_url, …)
                                                       ▼
[Django: agkit.io-backend]
  postback/raster/<field_id>/<token>/      ←── multipart (parameter, layer, file)
  postback/scalar/<field_id>/<token>/      ←── JSON (parameter, value, units, …)
```

## Local dev (uv)

```bash
uv sync --extra dev      # creates .venv, installs deps
uv run python -m unittest discover -v tests/
```

Pure-Python tests run locally without GDAL/GRASS — tests that need the
native libs (or DEM tile fixtures) are auto-skipped. The full suite
runs inside the Lambda image.

## Build

```bash
./build_deployment.sh                 # → agkit-topography:latest
```

## Local run (with RIE)

```bash
docker run -p 9000:8080 --read-only --mount type=tmpfs,destination=/tmp \
    agkit-topography:latest

curl -XPOST "http://localhost:9000/2015-03-31/functions/function/invocations" \
    -d @tests/fixtures/sqs_event.json
```

## Tests (inside Lambda image)

```bash
docker run --rm -v "$PWD:/code" agkit-topography:latest python -m unittest discover -v
```

Test fixtures requiring DEM tiles expect these files in `tests/`:
- `USGS_13_n34w088.tif`, `USGS_13_n40w088.tif`, `USGS_13_n40w089.tif`,
  `USGS_13_n41w089.tif`, `USGS_13_n42w092.tif`.
Download from: `https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/{tile}/USGS_13_{tile}.tif`.

## Named geo functions

Set on each job's `metadata.function_name`. Must match a method on
`LambdaGISProcessor` (see `app/geoworker.py`).

| function_name                       | kind    | postback   |
|-------------------------------------|---------|------------|
| `elev_public_10m`                   | raster  | raster     |
| `slope_public_10m`                  | raster  | raster     |
| `watershed_drainage`                | raster  | raster     |
| `watershed_streambeds`              | raster  | raster     |
| `watershed_spi`                     | raster  | raster     |
| `watershed_tci`                     | raster  | raster     |
| `watershed_length_slope_raster`     | raster  | raster (new) |
| `watershed_slope_steepness_raster`  | raster  | raster (new) |
| `watershed_length_slope`            | scalar  | scalar (new) |
| `watershed_slope_steepness`         | scalar  | scalar (new) |
| `rasterize_and_colorize`            | raster  | raster     |

See `agkit.io-backend/tier2apps/topography/schema.py` for the canonical
constants on the Django side.
