"""Probe: run the field-aware MFD flowline pipeline on Gary's East and
emit QGIS-ready GeoJSON clipped to the field boundary (the shipping product:
lines to field + 50 ft, zones to the field exactly).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from osgeo import ogr, osr  # noqa: E402
from app.grass_handler import get_mfd_flowlines_raw  # noqa: E402

DEM = "tests/gary_dem_buffered.tif"
FIELD = "tests/gary_east_boundary.geojson"
OUTDIR = "tests/out_gary"
os.makedirs(OUTDIR, exist_ok=True)

ALBERS = ("+proj=aea +lat_1=29.5 +lat_2=45.5 +lat_0=23 +lon_0=-96 +x_0=0 +y_0=0 "
          "+ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs")
LONLAT = "+proj=longlat +ellps=WGS84 +datum=WGS84 +no_defs"

_S = osr.SpatialReference(); _S.ImportFromProj4(LONLAT)
_D = osr.SpatialReference(); _D.ImportFromProj4(ALBERS)
_TO_P = osr.CoordinateTransformation(_S, _D)
_TO_G = osr.CoordinateTransformation(_D, _S)


def _field_geoms():
    """Field polygon and field+50 ft, as EPSG:4326 OGR geometries."""
    ds = ogr.Open(FIELD)
    lyr = ds.GetLayer()
    feat = lyr.GetNextFeature()
    g = feat.GetGeometryRef().Clone()
    buf = g.Clone()
    buf.Transform(_TO_P)
    buf = buf.Buffer(50 * 0.3048)
    buf.Transform(_TO_G)
    return g, buf


def _clip(wkt, cutline):
    g = ogr.CreateGeometryFromWkt(wkt)
    if g is None or g.IsEmpty():
        return None
    c = g.Intersection(cutline)
    return None if c is None or c.IsEmpty() else c


def _planar_len_ft(geom):
    g = geom.Clone(); g.Transform(_TO_P)
    return g.Length() / 0.3048


def _planar_area_ac(geom):
    g = geom.Clone(); g.Transform(_TO_P)
    return g.GetArea() / 4046.8564224


raw = get_mfd_flowlines_raw(
    DEM, OUTDIR, field_shp=FIELD,
    options={"max_lines": 5, "min_flow_accumulation_cells": 50,
             "min_line_length_ft": 100},
)
field, field_buf = _field_geoms()
hill = [r for r in raw if r.get("kind") != "draw"]
draws = [r for r in raw if r.get("kind") == "draw"]

line_feats, zone_feats, draw_feats = [], [], []
print(f"\n{len(hill)} hillslopes, {len(draws)} raw draw segments")

for r in hill:
    line = _clip(r["line_wkt"], field_buf)
    zone = _clip(r["zone_wkt"], field)
    if line is None:
        continue
    props = {"kind": "hillslope", "rank": r["rank"],
             "flow_accumulation_cells": r["flow_accumulation_cells"]}
    line_feats.append({"type": "Feature", "properties": props,
                       "geometry": json.loads(line.ExportToJson())})
    if zone is not None:
        zone_feats.append({"type": "Feature", "properties": props,
                           "geometry": json.loads(zone.ExportToJson())})
    za = _planar_area_ac(zone) if zone is not None else 0.0
    print(f"  hillslope {r['rank']}: line {_planar_len_ft(line):7.1f} ft | "
          f"zone {za:6.2f} ac")

draw_len_ft = 0.0
for r in draws:
    line = _clip(r["line_wkt"], field_buf)
    if line is None:
        continue
    draw_len_ft += _planar_len_ft(line)
    draw_feats.append({
        "type": "Feature",
        "properties": {"kind": "draw", "rank": r["rank"],
                       "flow_accumulation_cells": r["flow_accumulation_cells"]},
        "geometry": json.loads(line.ExportToJson()),
    })
print(f"  draws: {len(draw_feats)} in-field segments, "
      f"{draw_len_ft:.0f} ft of channel total")

for name, feats in (("flowlines", line_feats), ("catchments", zone_feats),
                    ("draws", draw_feats)):
    with open(os.path.join(OUTDIR, f"gary_{name}.geojson"), "w") as fh:
        json.dump({"type": "FeatureCollection", "features": feats}, fh)
print(f"\nwrote gary_flowlines({len(line_feats)}) "
      f"gary_catchments({len(zone_feats)}) gary_draws({len(draw_feats)}) "
      f"to {OUTDIR}/  (clipped to field)")
