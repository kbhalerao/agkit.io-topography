"""
PNG colorization wrapper around `gdaldem color-relief`.

Ported from USGS2021. The colorize step writes a `color.txt` temp file
into the working folder, then runs gdaldem via the Python API.

`blended_topo()` is a PIL-free port of LabCore's
`ColorizeMixin.topo_colorize` — the "USGS 10m topography layer": a
multidirectional hillshade composited over a color-relief elevation map.
"""
from __future__ import annotations

import os

import numpy as np
from osgeo import gdal

from app.color_schemes import colors


# LabCore's 33-stop elevation ramp for the blended topo layer. Alpha 168 so
# the satellite basemap reads through the overlay. Ported verbatim from
# labcore/clients/gis/gishelper_colorize.py:ColorizeMixin.topo_colorize.
_TOPO_ELEVATION_RAMP = """nv 0 0 0 0
0% 0 29 192 168
3.125% 9 59 213 168
6.25% 18 89 233 168
9.375% 28 117 244 168
12.5% 36 145 250 168
15.625% 23 178 253 168
18.75% 10 204 233 168
21.875% 4 224 214 168
25% 23 225 203 168
28.125% 42 226 191 168
31.25% 62 226 179 168
34.375% 77 236 147 168
37.5% 90 249 109 168
40.625% 125 249 79 168
43.75% 181 236 57 168
46.875% 232 223 38 168
50% 239 211 45 168
53.125% 246 197 51 168
56.25% 255 175 50 168
59.375% 251 147 55 168
62.5% 246 118 60 168
65.625% 240 78 52 168
68.75% 235 38 36 168
71.875% 236 21 20 168
75% 236 4 4 168
78.125% 242 25 25 168
81.25% 249 61 61 168
84.375% 255 95 95 168
87.5% 255 119 119 168
90.625% 255 144 144 168
93.75% 255 168 168 168
96.875% 255 193 193 168
100% 255 218 218 168"""

# Foreground alpha for the grey hillshade, ported from LabCore's grey ramp.
_HILLSHADE_ALPHA = 60.0
# Gamma applied to the hillshade before compositing — deepens shadows.
# LabCore: `(A / 255.) ** (1 / 0.5)`, i.e. an exponent of 2.0.
_HILLSHADE_GAMMA_EXP = 2.0


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

    def blended_topo(self, elev_src: str) -> str:
        """
        Build the blended topography PNG: a multidirectional hillshade
        composited over a color-relief elevation map, 4x lanczos-upscaled.

        PIL-free port of LabCore's `ColorizeMixin.topo_colorize` — numpy
        alpha-compositing replaces `PIL.Image.alpha_composite`, and the gdal
        Python API replaces the `gdaldem` / `gdal_calc.py` CLI hops.

        `elev_src` is a single-band elevation raster in EPSG:4326. Returns
        the path to the produced RGBA PNG (`{elev_src}_blended.png`).
        """
        # 1. Multidirectional hillshade. The DEM is geographic (degrees), and
        #    `-s` is deliberately omitted: leaving the degree-scale horizontal
        #    units unscaled against the metre elevations applies a large
        #    vertical exaggeration. That is the point — at true scale, gentle
        #    farmland relief is invisible; the exaggeration is what reveals
        #    the micro-topography. Matches LabCore's topo_colorize.
        #    `-compute_edges` avoids a nodata border ring. Flags go through
        #    the options list — the same mechanism colorize() uses.
        hillshade_tif = f"{elev_src}_hs.tif"
        elev_ds = gdal.Open(elev_src)
        try:
            gdal.DEMProcessing(
                destName=hillshade_tif,
                srcDS=elev_ds,
                processing="hillshade",
                format="GTiff",
                options=["-q", "-multidirectional", "-alt", "45", "-compute_edges"],
            )
        finally:
            elev_ds = None

        # 2. Color-relief the elevation → RGBA GTiff (alpha 168 on data, 0 on
        #    nodata). Same invocation as colorize(), GTiff instead of PNG.
        ramp_txt = os.path.join(self.folder, "topo_ramp.txt")
        with open(ramp_txt, "w") as f:
            f.write(_TOPO_ELEVATION_RAMP)
        topo_tif = f"{elev_src}_topo.tif"
        elev_ds = gdal.Open(elev_src)
        try:
            gdal.DEMProcessing(
                destName=topo_tif,
                srcDS=elev_ds,
                processing="color-relief",
                format="GTiff",
                colorFilename=ramp_txt,
                options=["-q", "-alpha"],
            )
        finally:
            elev_ds = None

        # 3. Read both rasters — they share the elevation's grid.
        hs_ds = gdal.Open(hillshade_tif)
        hillshade = hs_ds.GetRasterBand(1).ReadAsArray().astype(np.float64)
        topo_ds = gdal.Open(topo_tif)
        background = topo_ds.ReadAsArray().astype(np.float64)  # (4, H, W) RGBA

        # 4. Foreground = gamma-corrected grey hillshade, constant alpha 60,
        #    transparent where the hillshade has no data (0).
        gamma = np.clip(hillshade / 255.0, 0.0, 1.0) ** _HILLSHADE_GAMMA_EXP
        grey = gamma * 255.0
        fg_rgb = np.stack([grey, grey, grey])                          # (3, H, W)
        fg_a = np.where(hillshade > 0, _HILLSHADE_ALPHA / 255.0, 0.0)   # (H, W)

        # 5. Background = color-relief elevation; its alpha band carries 168
        #    on data, 0 on nodata.
        bg_rgb = background[:3]
        bg_a = background[3] / 255.0

        # 6. Alpha-composite foreground OVER background (Porter-Duff "over").
        out_a = fg_a + bg_a * (1.0 - fg_a)
        safe_a = np.where(out_a > 0, out_a, 1.0)
        out_rgb = (fg_rgb * fg_a + bg_rgb * bg_a * (1.0 - fg_a)) / safe_a

        rgba = np.concatenate([out_rgb, (out_a * 255.0)[None]])
        rgba = np.clip(np.rint(rgba), 0, 255).astype(np.uint8)

        # 7. Write the composite as a GTiff so the upscale keeps georef.
        height, width = hillshade.shape
        composite_tif = f"{elev_src}_blended.tif"
        driver = gdal.GetDriverByName("GTiff")
        comp_ds = driver.Create(composite_tif, width, height, 4, gdal.GDT_Byte)
        comp_ds.SetGeoTransform(hs_ds.GetGeoTransform())
        comp_ds.SetProjection(hs_ds.GetProjection())
        for band_idx in range(4):
            comp_ds.GetRasterBand(band_idx + 1).WriteArray(rgba[band_idx])
        comp_ds = None
        hs_ds = None
        topo_ds = None

        # 8. 4x lanczos upscale → final RGBA PNG.
        out_png = f"{elev_src}_blended.png"
        gdal.Translate(
            out_png,
            composite_tif,
            format="PNG",
            width=width * 4,
            height=height * 4,
            resampleAlg="lanczos",
        )
        return out_png
