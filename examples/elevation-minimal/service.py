"""
Synchronous elevation endpoint: POST a GeoJSON boundary, get a PNG back.

This is a reference wrapper showing the HTTP surface a colleague (or the x402
gateway) would call. The actual work is `app.sync_elevation.render_elevation`;
this file only turns HTTP into a function call and back. GDAL lives in the
image — the caller needs no geospatial dependencies.

    # from the repo root, inside the Lambda image (GDAL present):
    uvicorn examples.elevation-minimal.service:app --port 8040   # us-west-2

    curl -sS -X POST http://localhost:8040/elevation \
         -H 'content-type: application/json' \
         --data '{"type":"Polygon","coordinates":[[[-123.30,44.55],
                  [-123.29,44.55],[-123.29,44.56],[-123.30,44.56],
                  [-123.30,44.55]]]}' \
         -o field.png -D -

The response body is the raw PNG. A color-relief PNG is never georeferenced,
so the true bounds ride back in the `X-Extent` header as JSON
`[west, south, east, north]` — what a MapLibre/Leaflet image overlay needs.

The public path is `/elevation`; the x402 metering prefix
(`METERING_CATALOG_PATH_PREFIX=/x402/v1/topo`) namespaces it to the catalog item
`/x402/v1/topo/elevation`. The Lambda's own handler (`app/http_handler.py`) does
this gating + billing itself via `app/metering.py`; this FastAPI file is just a
local reference for the same HTTP shape. See README for the catalog wiring.
"""
import json

from fastapi import FastAPI, HTTPException, Request, Response

from app.sync_elevation import render_elevation

app = FastAPI(title="USGS elevation (sync)")


@app.post("/elevation")
async def elevation(request: Request) -> Response:
    boundary = await request.json()
    try:
        result = render_elevation(boundary)
    except ValueError as exc:            # bad or oversized boundary
        raise HTTPException(status_code=422, detail=str(exc))
    except RuntimeError as exc:          # DEM read / off-coverage
        raise HTTPException(status_code=502, detail=str(exc))

    return Response(
        content=result["png"],
        media_type="image/png",
        headers={"X-Extent": json.dumps(result["extent"])},
    )


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}
