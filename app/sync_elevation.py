"""
Synchronous elevation renderer — boundary in, colorized PNG bytes out.

This is the async topography engine's elevation job (`geoworker.elev_public_10m`)
distilled to its synchronous core: read the USGS 10 m DEM window under a
boundary as a Cloud-Optimized GeoTIFF, colorize it, return the bytes. No GRASS,
no flow accumulation, no SQS, no postback — elevation coloring is cheap enough
to run inside a request/response.

Everything runs in GDAL's in-memory `/vsimem/` filesystem, so there are no temp
files and nothing touches disk.

Why it can be synchronous (unlike the watershed jobs): the DEM tiles in
s3://prd-tnm are valid COGs (256x256 internal tiling + overviews), so GDAL's
`/vsis3/` driver issues HTTP range reads for only the blocks overlapping the
boundary window — a field-scale read is a few hundred KB to a few MB, not the
~440 MB whole tile. Run this in us-west-2 (co-located with prd-tnm) and it's
sub-2s. The reads are unsigned (`AWS_NO_SIGN_REQUEST`): prd-tnm allows
anonymous access and the execution role has no grant on it, so a *signed*
request 403s. See CLAUDE.md "USGS DEM access".

Intended as the compute behind an x402-metered HTTP endpoint: GDAL lives in our
image, the caller just POSTs a boundary and gets PNG bytes — zero geospatial
deps on their side.
"""
from __future__ import annotations

import math
import tempfile

from osgeo import gdal

from app import settings
from app.color_schemes import colors

gdal.UseExceptions()


# USGS public DEM dataset (us-west-2). Tiles are named by NW corner:
# n42w092 covers 41-42 N, 92-91 W, keyed at .../current/n42w092/USGS_13_n42w092.tif
DEM_BUCKET = settings.USGS_13_DEM_BUCKET
DEM_KEY_PREFIX = settings.USGS_13_KEY_PREFIX.rstrip("/")

# Unsigned COG reads (see module docstring). Applied per-call via
# gdal.config_options so we never mutate global GDAL state.
_VSIS3_OPTS = {
    "AWS_NO_SIGN_REQUEST": "YES",
    "AWS_REGION": settings.AWS_REGION,
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "1",
    # Skip the bucket directory LIST that GDAL does on first open — on the huge
    # anonymous prd-tnm bucket that LIST is what made a cold /vsis3 open take
    # ~20s (and 503 the endpoint). EMPTY_DIR opens the file directly.
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    # Don't probe for sidecars (.ovr/.aux.xml/...): the USGS COGs carry internal
    # overviews, so only ever fetch the .tif.
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "VSI_CACHE": "TRUE",
}

# Small buffer (deg) so the image isn't clipped hard to the fenceline.
_BUFFER_DEG = 0.003

# Refuse boundaries whose bbox spans more than this many 1-degree tiles.
# Field / HUC-12 scale is 1 tile; this stops a whole-state polygon from
# blowing the synchronous latency + memory budget.
MAX_TILES = 4


def _tile_name(lat_idx: int, lon_idx: int) -> str:
    """USGS NW-corner tile name, e.g. (42, 92) -> 'n42w092'."""
    return f"n{lat_idx:02d}w{lon_idx:03d}"


def _tile_source(name: str) -> str:
    """Resolve a tile name to a GDAL-openable source.

    In IN_TEST mode, read the local fixture (`tests/USGS_13_<name>.tif`) so the
    suite runs offline — mirrors `ziphandler.download_USGS_dem` /
    `geoworker.get_dem`. Otherwise, the `/vsis3/` COG on prd-tnm.
    """
    fname = f"USGS_13_{name}.tif"
    if settings.IN_TEST:
        return f"tests/{fname}"
    return f"/vsis3/{DEM_BUCKET}/{DEM_KEY_PREFIX}/{name}/{fname}"


def bbox_of_geojson(geojson: dict) -> tuple[float, float, float, float]:
    """(west, south, east, north) over every coordinate in a GeoJSON Feature,
    FeatureCollection, or bare geometry. EPSG:4326."""

    def walk(coords, acc):
        if coords and isinstance(coords[0], (int, float)):
            lon, lat = coords[0], coords[1]
            acc[0], acc[1] = min(acc[0], lon), min(acc[1], lat)
            acc[2], acc[3] = max(acc[2], lon), max(acc[3], lat)
        else:
            for c in coords:
                walk(c, acc)

    if geojson.get("type") == "FeatureCollection":
        geoms = [f["geometry"] for f in geojson["features"]]
    elif geojson.get("type") == "Feature":
        geoms = [geojson["geometry"]]
    else:
        geoms = [geojson]

    acc = [math.inf, math.inf, -math.inf, -math.inf]
    for g in geoms:
        walk(g["coordinates"], acc)
    if acc[0] is math.inf:
        raise ValueError("boundary contained no coordinates")
    return tuple(acc)  # type: ignore[return-value]


def tiles_for_bbox(west, south, east, north) -> list[str]:
    """Sources for every 1-degree DEM tile the bbox touches.

    Tile L (latitude) covers [L-1, L]; tile W (west longitude, positive)
    covers [-W, -W+1]. So the NW-corner index of the tile holding a point is
    lat=ceil(lat), lon=ceil(-lon)."""
    lat_idxs = range(math.ceil(south), math.ceil(north) + 1)
    lon_idxs = range(math.ceil(-east), math.ceil(-west) + 1)
    return [
        _tile_source(_tile_name(lat, lon))
        for lat in lat_idxs
        for lon in lon_idxs
    ]


def _read_vsimem(path: str) -> bytes:
    """Slurp a `/vsimem/` file into bytes."""
    f = gdal.VSIFOpenL(path, "rb")
    if f is None:
        raise RuntimeError(f"could not open in-memory file {path}")
    try:
        gdal.VSIFSeekL(f, 0, 2)  # SEEK_END
        size = gdal.VSIFTellL(f)
        gdal.VSIFSeekL(f, 0, 0)  # SEEK_SET
        return bytes(gdal.VSIFReadL(1, size, f))
    finally:
        gdal.VSIFCloseL(f)


def _extent_of(ds) -> list[float]:
    """[west, south, east, north] from a north-up 4326 dataset's geotransform.
    The clipped window snaps to pixel edges, so this is the artifact's true
    bounds — which travel alongside the georef-less PNG."""
    gt = ds.GetGeoTransform()
    nx, ny = ds.RasterXSize, ds.RasterYSize
    west, north = gt[0], gt[3]
    east = gt[0] + nx * gt[1]
    south = gt[3] + ny * gt[5]
    return [west, south, east, north]


# A real, always-present Oregon tile used only to warm the /vsis3 path on
# /prime. Any valid tile works — the win is priming GDAL's S3/CURL/driver state,
# which is process-global and transfers to real renders of other tiles.
_PRIME_TILE = "n45w124"


def prime() -> dict:
    """Warm the process so the next render skips the cold first-access cost.

    A cold container's first render is dominated not by the render logic (which
    is ~100 ms warm) but by the first `/vsis3` access to prd-tnm — CURL/TLS/DNS
    setup, the S3 region probe, and COG codec init. This opens one tile and
    reads a small overview window to pay that cost up front. All of it is
    process-global, so it transfers to subsequent renders of *any* boundary.

    Bounded on purpose (one tile, a ≤64 px overview read, no full-res clip) so
    `/prime` itself returns well under the 30 s API Gateway ceiling even cold.
    """
    src = _tile_source(_PRIME_TILE)
    with gdal.config_options(_VSIS3_OPTS):
        ds = gdal.Open(src)
        if ds is None:
            raise RuntimeError(f"prime: could not open {src}")
        band = ds.GetRasterBand(1)
        n_ov = band.GetOverviewCount()
        read_band = band.GetOverview(n_ov - 1) if n_ov else band
        w, h = min(64, read_band.XSize), min(64, read_band.YSize)
        read_band.ReadAsArray(0, 0, w, h)  # one real (small) block transfer
        raster_size = [ds.RasterXSize, ds.RasterYSize]
        ds = None
    return {"primed": True, "tile": _PRIME_TILE, "raster_size": raster_size}


def render_elevation(boundary_geojson: dict) -> dict:
    """Boundary in, colorized elevation PNG out.

    Returns::

        {
          "png": <bytes>,                        # RGBA color-relief PNG (display)
          "tif": <bytes>,                        # single-band Float32 GeoTIFF (data)
          "extent": [west, south, east, north],  # true bounds, EPSG:4326
          "width": int, "height": int,           # pixel dims
        }

    The PNG is for display; the GeoTIFF is the actual clipped elevation data
    (georeferenced, so a client can read values / reproject). The PNG carries
    no georeferencing (color-relief drops it), so `extent` travels alongside it
    — a map client needs it to place the image. Both share `extent`.

    Raises ValueError on a bad / oversized boundary, RuntimeError on read.
    """
    west, south, east, north = bbox_of_geojson(boundary_geojson)
    west, south = west - _BUFFER_DEG, south - _BUFFER_DEG
    east, north = east + _BUFFER_DEG, north + _BUFFER_DEG

    tiles = tiles_for_bbox(west, south, east, north)
    if len(tiles) > MAX_TILES:
        raise ValueError(
            f"boundary spans {len(tiles)} DEM tiles (max {MAX_TILES}); "
            "area too large for a synchronous render"
        )

    with gdal.config_options(_VSIS3_OPTS):
        # 1. Stitch the touched tiles into one virtual mosaic (a no-op wrapper
        #    for the common single-tile case; makes multi-tile Just Work).
        vrt = gdal.BuildVRT("/vsimem/dem.vrt", tiles)
        if vrt is None:
            raise RuntimeError("BuildVRT produced no dataset (DEM read failed)")

        # 2. Clip to the boundary window. projWin is [ulx, uly, lrx, lry] =
        #    [west, north, east, south]; only overlapping COG blocks transfer.
        clip = gdal.Translate(
            "/vsimem/clip.tif",
            vrt,
            projWin=[west, north, east, south],
            noData=0,
            outputType=gdal.GDT_Float32,
        )
        vrt = None
        if clip is None:
            raise RuntimeError("clip produced no data — boundary off-coverage?")
        extent = _extent_of(clip)
        width, height = clip.RasterXSize, clip.RasterYSize

    # 3. Colorize -> RGBA PNG. gdaldem wants the ramp as a real file path; the
    #    color file is tiny, so a NamedTemporaryFile is fine. The "elevation"
    #    ramp's percentile stops auto-stretch to the window's own min/max.
    with tempfile.NamedTemporaryFile("w", suffix=".txt") as ramp:
        ramp.write(colors["elevation"])
        ramp.flush()
        gdal.DEMProcessing(
            "/vsimem/color.png",
            clip,
            processing="color-relief",
            format="PNG",
            colorFilename=ramp.name,
            addAlpha=True,
        )
    clip = None  # flush the GeoTIFF to /vsimem before we slurp it

    png = _read_vsimem("/vsimem/color.png")
    tif = _read_vsimem("/vsimem/clip.tif")

    # Don't leak the in-memory files across calls in a long-lived server.
    for p in ("/vsimem/dem.vrt", "/vsimem/clip.tif",
              "/vsimem/color.png", "/vsimem/color.png.aux.xml"):
        gdal.Unlink(p)

    return {"png": png, "tif": tif, "extent": extent,
            "width": width, "height": height}
