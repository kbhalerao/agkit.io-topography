# Blended topography layer — feature doc

The "USGS 10m topography layer": a multidirectional hillshade composited
over a color-relief elevation map, delivered as a single finished RGBA PNG.
A basemap layer — it gives a field a readable relief backdrop that the
satellite imagery and the data overlays (slope, drainage, LS) sit on top of.

**Status: implemented.** `topo_blended_public_10m` in `app/geoworker.py`,
`GeoColorize.blended_topo()` in `app/geocolorize.py`. Shipped in PR #1
(`feat/blended-topo-hillshade`).

This is a PIL-free port of LabCore's `ColorizeMixin.topo_colorize`
(`labcore/clients/gis/gishelper_colorize.py`) — numpy alpha-compositing
replaces `PIL.Image.alpha_composite`, and the GDAL Python API replaces the
`gdaldem` / `gdal_calc.py` CLI hops. The output is verified against the
LabCore reference blend.

## Output contract

- **One RGBA PNG per field.** No data GeoTIFF — unlike the elevation and
  slope layers, this is a finished picture, not a numeric raster.
- The PNG has **no georeferencing**. Its true bounds travel on the postback
  as `extent` (`[west, south, east, north]`, EPSG:4326), derived from the
  clipped elevation source tif. Django stores `extent` on the layer; the
  frontend overlays the image at those exact bounds.
- Posted through the standard `postback/raster/...` endpoint via
  `_post_built_raster()` — which, unlike `handle_posting()`, does **no**
  colorize step, because the handler already produced the finished PNG.

## Pipeline (`GeoColorize.blended_topo`)

Input is a single-band elevation raster in EPSG:4326 (the field-clipped
DEM). Steps:

1. **Multidirectional hillshade** — `gdal.DEMProcessing` `hillshade` with
   `-multidirectional -alt 45 -compute_edges`. `-compute_edges` avoids a
   nodata border ring.
2. **Color-relief elevation** — `gdal.DEMProcessing` `color-relief` with
   `-alpha`, using LabCore's 33-stop elevation ramp (`_TOPO_ELEVATION_RAMP`,
   ported verbatim). Alpha 168 on data so a satellite basemap reads through;
   alpha 0 on nodata.
3. **Foreground** — gamma-correct the grey hillshade (`exponent 2.0`,
   deepens shadows), then **min-max contrast-stretch** the valid pixels back
   to 0–255. Constant alpha 60, transparent where the hillshade has no data.
4. **Composite** — Porter-Duff "over": foreground hillshade over background
   color-relief, numpy alpha-compositing.
5. **Upscale** — write the composite as a GeoTIFF (keeps georef for the
   `extent` computation), then `gdal.Translate` 4× with lanczos resampling
   to the final RGBA PNG.

### Two deliberate choices (test findings — see commit history)

- **No hillshade `-s` scale.** Scaling the geographic (degree) DEM to true
  metric proportions (`-s 111120`) is geographically correct but washes the
  relief flat — gentle farmland micro-topography becomes invisible. The
  unscaled degree/metre mismatch is a *deliberate vertical exaggeration*.
  Matches LabCore's `topo_colorize`.
- **Contrast-stretch the foreground.** LabCore applies the stretch
  implicitly via a 0%–100% grey color-relief ramp. The numpy port has to do
  it explicitly (step 3) — without it the shaded relief reads as a muddy
  mid-grey and ridges lose definition.

## Dependency note

`numpy<2` is pinned (`requirements.txt`, `pyproject.toml`). NumPy 2.x is
ABI-incompatible with the base image's GDAL `gdal_array` extension (built
against NumPy 1.x) — every `ReadAsArray()` / `WriteArray()` crashes on
import otherwise. This affects the scalar reducers too, not just this layer.

## Consuming from a daughter project

Add a job to `AWS_LAMBDA_EVENT_CONFIGURATION` on the Django side with
`lambda_function: "topo_blended_public_10m"` and a single `png` output
slot. The handler builds the clipped DEM via `build_common_cache()` and
posts one PNG back. Nothing else on the Django side differs from a normal
raster layer — the PNG is stored against the field with its `extent`.

## What this is not

- **Not a data layer.** No numeric raster, no scalar reduction. Use
  `elev_public_10m` if you need the elevation values.
- **Not georeferenced.** The PNG is a picture; `extent` is the only
  placement information. Do not assume the file has a CRS.
