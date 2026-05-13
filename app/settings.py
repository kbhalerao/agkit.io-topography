"""
Minimal Lambda settings.

Replaces USGS2021's `lambda_settings.py`. The Django credentials that
shipped with the old image are gone — postbacks are authenticated by
the magic-signed `post_url` in each job, not by Lambda-side credentials.

Lambda only needs:
* AWS credentials *to read SQS / write to its own logs*, which it gets
  from its execution role at runtime (no static keys in env).
* The USGS DEM bucket / key prefix (these are constants — public dataset).
* `IN_TEST` to switch off network hops for unit tests.
"""
import os


# ---------------------------------------------------------------------------
# USGS public DEM dataset (us-west-2). Co-located with the Lambda → free read.
# ---------------------------------------------------------------------------
USGS_13_DEM_BUCKET = "prd-tnm"
USGS_13_DEM_URL_PREFIX = f"https://{USGS_13_DEM_BUCKET}.s3.amazonaws.com/"
USGS_13_KEY_PREFIX = "StagedProducts/Elevation/13/TIFF/current/"
# Example: https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current/n41w088/USGS_13_n41w088.tif


# ---------------------------------------------------------------------------
# Runtime knobs
# ---------------------------------------------------------------------------
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
TEMP_FILE_PATH = os.environ.get("TEMP_FILE_PATH", "/tmp")

IN_TEST = os.environ.get("IN_TEST", "false").lower() == "true"

# Postback HTTP timeouts (seconds). The Django side does the heavy work
# (raster file persist), so keep this generous but not unbounded.
POSTBACK_TIMEOUT_SECONDS = int(os.environ.get("POSTBACK_TIMEOUT_SECONDS", "60"))
