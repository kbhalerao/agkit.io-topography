"""
File-fetch helpers.

Ported from USGS2021's `downloader.py`. The `file_downloader` helper is
retained for the rare paths that still pull a sidecar file from S3
(e.g., `rasterize_and_colorize` input zips). The boto3 client is
configured for the Lambda's own region and inherits credentials from
the execution role — no static keys.

Note: USGS DEM tiles are no longer fetched here. They are read in
place over GDAL's `/vsis3/` (see `ziphandler.download_USGS_dem`), so
there is no boto3 client for the public `prd-tnm` bucket.
"""
import os
from shutil import copyfile

import boto3

from app import settings


_s3_client = None


def s3_client():
    """Lazy boto3 S3 client (region from execution env)."""
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3", region_name=settings.AWS_REGION)
    return _s3_client


def file_downloader(bucket: str, key: str, download_path: str) -> None:
    """Download `s3://{bucket}/{key}` to `download_path`."""
    if settings.IN_TEST:
        # In tests we expect a local fixture file under tests/ with the
        # same basename as the key.
        copyfile(os.path.join("tests", os.path.basename(key)), download_path)
        return
    s3_client().download_file(bucket, key, download_path)


def get_path(folder: str, layer: str):
    """Locate `layer` (filename) recursively inside `folder`."""
    for root, _subdirs, files in os.walk(folder):
        if layer in files:
            return os.path.abspath(os.path.join(root, layer))
    return None
