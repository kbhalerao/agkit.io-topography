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
├── geocolorize.py    # PNG colorization via gdaldem color-relief
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

## Adding a new geo function

1. Add a method to `LambdaGISProcessor` in `app/geoworker.py`. Method
   signature is `def function_name(self, payload):`.
2. Use `self.build_common_cache(payload)` to access shared field
   shapefile, DEM raster, and clipped DEM raster.
3. For raster jobs, return `self.handle_posting(file, color_map, payload)`.
   For scalar jobs, compute the scalar and call `Poster.post_scalar(...)`.
4. Add a color ramp to `app/color_schemes.py` if needed.
5. On the Django side, add the parameter constant to
   `tier2apps/topography/schema.py` and the job to
   `settings.AWS_LAMBDA_EVENT_CONFIGURATION`.

## Cross-project references

- Backend topography app: `agkit.io-backend/tier2apps/topography/`
- Backend service that builds the event JSON: `services.py:LambdaEventBuilder`
- Backend postback views: `api_views.py:RasterPostbackView` / `ScalarPostbackView`
- Backend magic-link signing: `tier1apps/foundations/magic_signing.py`
