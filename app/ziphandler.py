"""
USGS DEM tile retrieval + supporting zip helpers.

Ported from USGS2021. DEM tiles are pulled directly from the public
`prd-tnm` bucket (us-west-2) — Lambda is co-located, so same-region
S3 reads are free and no caching is needed.
"""
import os
import tempfile
import time
import zipfile

from osgeo import gdal, osr

from app import settings
from app.downloader import file_downloader


def download_USGS_dem(s3_client, dem_folder, dem_tile_lat, dem_tile_long, field_extent=None):
    """
    Download a single USGS 1/3 arc-second DEM tile to `dem_folder`.

    Tile naming follows the northwest-corner convention: a tile at
    (lat=41, long=88) covers 40°-41°N, 88°-87°W and is keyed
    `n41w088`. If `field_extent` is provided, the tile is clipped to
    the field extent (+ a small buffer) in-place via `gdal.Translate`.
    """
    dem_tile_long = ("0" + str(dem_tile_long))[-3:]
    if not os.path.exists(dem_folder):
        os.mkdir(dem_folder)

    folder = f"n{dem_tile_lat}w{dem_tile_long}"
    file = f"USGS_13_{folder}.tif"
    key = f"{settings.USGS_13_KEY_PREFIX}{folder}/{file}"

    print(f"Downloading DEM tile from USGS: {key}")
    start = time.perf_counter()
    if not settings.IN_TEST:
        s3_response_object = s3_client.get_object(
            Bucket=settings.USGS_13_DEM_BUCKET, Key=f"{key}",
        )
        object_content = s3_response_object["Body"].read()
        print(f"Downloaded from USGS prd-tnm, took {time.perf_counter() - start:.2f}s")
    else:
        print("In test: using local file")
        with open(f"tests/{file}", "rb") as f:
            object_content = f.read()

    usgs_tif = os.path.join(dem_folder, file)
    if not os.path.exists(os.path.dirname(usgs_tif)):
        os.makedirs(os.path.dirname(usgs_tif), exist_ok=True)

    if field_extent is not None:
        # field_extent is (long_west, lat_north, long_east, lat_south).
        # gdal.Translate projWin wants [ulx, uly, lrx, lry].
        new_extent = [
            field_extent[0] - 0.003,
            field_extent[3] + 0.003,
            field_extent[2] + 0.003,
            field_extent[1] - 0.003,
        ]
        print("New extent for usgs tif from field extent:  ", new_extent)
        src_srs = osr.SpatialReference()
        res = src_srs.ImportFromProj4(
            "+proj=longlat +ellps=WGS84 +datum=WGS84 +no_defs",
        )
        if res != 0:
            raise RuntimeError(f"{res}: could not import EPSG:4326 from proj4")

        in_mem_dem = "/vsimem/dem.tif"
        gdal.FileFromMemBuffer(in_mem_dem, object_content)
        src_ds = gdal.Open(in_mem_dem)
        try:
            gdal.Translate(
                usgs_tif, src_ds,
                outputSRS=src_srs,
                noData=0,
                projWin=new_extent,
                format="GTiff",
                outputType=gdal.gdalconst.GDT_Float32,
                creationOptions=["COMPRESS=LZW"],
            )
        finally:
            src_ds = None
            gdal.Unlink(in_mem_dem)
    else:
        with open(usgs_tif, "wb") as f:
            f.write(object_content)

    return usgs_tif, [usgs_tif]


def handle_USGS_DEM(s3_client, dem_folder, dem_tile_lat, dem_tile_long, field_extent=None):
    """Wrapper that swallows download errors and returns a tif path or None."""
    try:
        usgs_tif, _ = download_USGS_dem(
            s3_client, dem_folder, dem_tile_lat, dem_tile_long, field_extent,
        )
    except Exception as exc:
        print("Error downloading USGS DEM, ", exc)
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
