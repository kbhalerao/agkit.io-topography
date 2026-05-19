"""
USGS DEM tile retrieval + supporting zip helpers.

Ported from USGS2021. DEM tiles live in the public `prd-tnm` bucket
(us-west-2). The tiles are valid Cloud-Optimized GeoTIFFs (256x256
internal tiling + overviews), so GDAL reads them in place over
`/vsis3/` with HTTP range requests — only the blocks overlapping the
requested window are transferred, not the full ~440 MB tile. No
caching needed; the Lambda is co-located with the bucket.
"""
import os
import tempfile
import zipfile

from osgeo import gdal

from app import settings
from app.downloader import file_downloader


# `prd-tnm` allows anonymous reads and the Lambda execution role has no
# IAM grant on it, so GDAL must not sign with the role's credentials.
# A handful of retries covers transient range-read hiccups.
_VSIS3_CONFIG = {
    "AWS_NO_SIGN_REQUEST": "YES",
    "AWS_REGION": settings.AWS_REGION,
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "1",
}


def download_USGS_dem(dem_folder, dem_tile_lat, dem_tile_long, field_extent=None):
    """
    Fetch a single USGS 1/3 arc-second DEM tile into `dem_folder`,
    clipped to `field_extent` if given.

    Tile naming follows the northwest-corner convention: a tile at
    (lat=41, long=88) covers 40°-41°N, 88°-87°W and is keyed
    `n41w088`. The source COG is read over `/vsis3/` and clipped with
    `gdal.Translate`, so a small `field_extent` only pulls the
    overlapping blocks rather than the whole tile.
    """
    dem_tile_long = ("0" + str(dem_tile_long))[-3:]
    os.makedirs(dem_folder, exist_ok=True)

    folder = f"n{dem_tile_lat}w{dem_tile_long}"
    file = f"USGS_13_{folder}.tif"
    key = f"{settings.USGS_13_KEY_PREFIX}{folder}/{file}"

    if settings.IN_TEST:
        print("In test: using local file")
        source = f"tests/{file}"
    else:
        source = f"/vsis3/{settings.USGS_13_DEM_BUCKET}/{key}"
        for opt, value in _VSIS3_CONFIG.items():
            gdal.SetConfigOption(opt, value)

    usgs_tif = os.path.join(dem_folder, file)

    translate_kwargs = dict(
        noData=0,
        format="GTiff",
        outputType=gdal.gdalconst.GDT_Float32,
        creationOptions=["COMPRESS=LZW"],
    )
    if field_extent is not None:
        # field_extent is (xmin, ymin, xmax, ymax) in EPSG:4326.
        # gdal.Translate projWin wants [ulx, uly, lrx, lry] — i.e.
        # [west, north, east, south] — with a small buffer added.
        translate_kwargs["projWin"] = [
            field_extent[0] - 0.003,
            field_extent[3] + 0.003,
            field_extent[2] + 0.003,
            field_extent[1] - 0.003,
        ]
        print("Clipping USGS tile to extent:  ", translate_kwargs["projWin"])

    print(f"Reading DEM tile: {source}")
    out_ds = gdal.Translate(usgs_tif, source, **translate_kwargs)
    if out_ds is None:
        raise RuntimeError(f"gdal.Translate produced no output for {source}")
    out_ds = None  # flush to disk

    return usgs_tif, [usgs_tif]


def handle_USGS_DEM(dem_folder, dem_tile_lat, dem_tile_long, field_extent=None):
    """Wrapper that swallows fetch errors and returns a tif path or None."""
    try:
        usgs_tif, _ = download_USGS_dem(
            dem_folder, dem_tile_lat, dem_tile_long, field_extent,
        )
    except Exception as exc:
        print("Error fetching USGS DEM, ", exc)
        return None
    return usgs_tif


def download_and_unzip(bucket: str, key: str):
    """Fetch a zip from S3 and extract it into a fresh tempdir."""
    if settings.IN_TEST:
        return os.path.abspath("tests/Garys/GarysEast2018")

    try:
        tempdir = tempfile.mkdtemp(prefix="zip")
        fname = f"{tempdir}/{os.path.basename(key)}"
        file_downloader(bucket, key, download_path=fname)
        with zipfile.ZipFile(fname, "r") as zipf:
            zipf.extractall(tempdir)
        return fname
    except Exception as exc:
        print("Unable to download_and_unzip", bucket, key, repr(exc))
        return None
