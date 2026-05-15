"""
PNG colorization wrapper around `gdaldem color-relief`.

Ported from USGS2021. The colorize step writes a `color.txt` temp file
into the working folder, then runs gdaldem via the Python API.
"""
from __future__ import annotations

import os

from osgeo import gdal

from app.color_schemes import colors


class GeoColorize:
    def __init__(self, folder: str | None = None):
        self.folder = folder or "/tmp/"

    def colorize(self, src: str, color_scheme: str) -> str:
        """
        Colorize `src` raster using a named scheme from `color_schemes.colors`.
        Returns the path to the produced PNG (named `{src}_col.png`).
        Falls back to the `default` scheme if `color_scheme` is unknown.
        """
        scheme = colors.get(color_scheme, colors["default"])
        color_txt = os.path.join(self.folder, "color.txt")
        with open(color_txt, "w") as f:
            f.write(scheme)

        out_png = f"{src}_col.png"
        src_ds = gdal.Open(src)
        try:
            gdal.DEMProcessing(
                destName=out_png,
                srcDS=src_ds,
                processing="color-relief",
                format="PNG",
                colorFilename=color_txt,
                options=["-q", "-alpha"],
            )
        finally:
            src_ds = None
        return out_png
