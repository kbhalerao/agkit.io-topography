"""Diagnostic: list every candidate hillslope on the buffered DEM, ranked,
with how much of each lies inside the Gary's East boundary."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from osgeo import ogr, osr  # noqa: E402
from app.grass_handler import get_mfd_flowlines_raw  # noqa: E402

DEM = "tests/gary_dem_buffered.tif"
FIELD = "tests/gary_east_boundary.geojson"
OUTDIR = "tests/out_gary_diag"
os.makedirs(OUTDIR, exist_ok=True)

ALBERS = ("+proj=aea +lat_1=29.5 +lat_2=45.5 +lat_0=23 +lon_0=-96 +x_0=0 +y_0=0 "
          "+ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs")
LONLAT = "+proj=longlat +ellps=WGS84 +datum=WGS84 +no_defs"
_S = osr.SpatialReference(); _S.ImportFromProj4(LONLAT)
_D = osr.SpatialReference(); _D.ImportFromProj4(ALBERS)
_TO_P = osr.CoordinateTransformation(_S, _D)


def _len_ft(geom):
    g = geom.Clone(); g.Transform(_TO_P)
    return g.Length() / 0.3048


ds = ogr.Open(FIELD)
_lyr = ds.GetLayer()
_feat = _lyr.GetNextFeature()
field = _feat.GetGeometryRef().Clone()

# field_shp=None → rank purely by total basin area, no field filter.
raw = get_mfd_flowlines_raw(
    DEM, OUTDIR, field_shp=None,
    options={"max_lines": 20, "min_flow_accumulation_cells": 50,
             "min_line_length_ft": 100},
)
import json  # noqa: E402

rows = []
feats = []
for r in raw:
    line = ogr.CreateGeometryFromWkt(r["line_wkt"])
    total = _len_ft(line)
    clip = line.Intersection(field)
    inf = _len_ft(clip) if clip and not clip.IsEmpty() else 0.0
    c = line.GetPoint(0)
    rows.append((total, inf, c, r["flow_accumulation_cells"]))
    feats.append({
        "type": "Feature",
        "geometry": json.loads(line.ExportToJson()),
        "properties": {
            "total_ft": round(total, 1),
            "in_field_pct": round(100 * inf / total, 1) if total else 0,
            "flow_accumulation_cells": r["flow_accumulation_cells"],
        },
    })
with open(os.path.join(OUTDIR, "gary_all_candidates.geojson"), "w") as fh:
    json.dump({"type": "FeatureCollection", "features": feats}, fh)

rows.sort(reverse=True)
print(f"\n{len(rows)} candidate hillslopes, sorted by total length:")
print(f"{'total_ft':>9} {'in-field_ft':>11} {'in-field%':>9}  start_lon,lat")
for total, inf, c, acc in rows:
    pct = 100 * inf / total if total else 0
    print(f"{total:9.0f} {inf:11.0f} {pct:8.0f}%  ({c[0]:.5f},{c[1]:.5f})  acc={acc}")
