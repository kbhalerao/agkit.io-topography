"""Sensitivity sweep: vary each key threshold on Gary's East and report
how the hillslope and draw output moves. Outputs are clipped to the field.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from osgeo import ogr, osr  # noqa: E402
from app.grass_handler import get_mfd_flowlines_raw  # noqa: E402

DEM = "tests/gary_dem_buffered.tif"
FIELD = "tests/gary_east_boundary.geojson"
OUTDIR = "tests/out_gary_sweep"
os.makedirs(OUTDIR, exist_ok=True)

ALBERS = ("+proj=aea +lat_1=29.5 +lat_2=45.5 +lat_0=23 +lon_0=-96 +x_0=0 +y_0=0 "
          "+ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs")
_S = osr.SpatialReference(); _S.ImportFromProj4(
    "+proj=longlat +ellps=WGS84 +datum=WGS84 +no_defs")
_D = osr.SpatialReference(); _D.ImportFromProj4(ALBERS)
_TO_P = osr.CoordinateTransformation(_S, _D)

_ds = ogr.Open(FIELD)
_lyr = _ds.GetLayer()
_feat = _lyr.GetNextFeature()
FIELD_GEOM = _feat.GetGeometryRef().Clone()

BASE = {"max_lines": 5, "min_flow_accumulation_cells": 50,
        "channel_threshold_cells": 50, "draw_threshold_cells": 300,
        "max_draws": 6}


def _len_ft(geom):
    g = geom.Clone(); g.Transform(_TO_P)
    return g.Length() / 0.3048


def run(label, **over):
    opts = dict(BASE, **over)
    raw = get_mfd_flowlines_raw(DEM, OUTDIR, field_shp=FIELD, options=opts)
    hill, draw = [], []
    for r in raw:
        g = ogr.CreateGeometryFromWkt(r["line_wkt"])
        clip = g.Intersection(FIELD_GEOM)
        if clip is None or clip.IsEmpty():
            continue
        (draw if r.get("kind") == "draw" else hill).append(_len_ft(clip))
    hill.sort(reverse=True)
    draw.sort(reverse=True)
    hs = " ".join(f"{x:.0f}" for x in hill) or "-"
    ds = " ".join(f"{x:.0f}" for x in draw) or "-"
    print(f"  {label:28s} | {len(hill)} hill [{hs}]  | {len(draw)} draw [{ds}]")


print("\nBASELINE: min_flow_acc=50 channel=50 draw=300\n")
print("  --- min_flow_accumulation_cells (r.watershed / half-basin size) ---")
for v in (25, 50, 100, 200):
    run(f"min_flow_acc={v}", min_flow_accumulation_cells=v,
        channel_threshold_cells=v)
print("  --- channel_threshold_cells (hillslope foot) ---")
for v in (25, 50, 100, 200):
    run(f"channel_threshold={v}", channel_threshold_cells=v)
print("  --- draw_threshold_cells (what counts as a draw) ---")
for v in (150, 300, 600, 1000):
    run(f"draw_threshold={v}", draw_threshold_cells=v)
