"""Slope-area analysis for Gary's East: does a channel-initiation kink
exist in the terrain? If so it sets channel_threshold and its bracket."""
import os
import shutil
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
from app.grass_handler import initialize_grassdb, _rasterize_field  # noqa: E402

DEM = "tests/gary_dem_buffered.tif"
FIELD = "tests/gary_east_boundary.geojson"
OUTDIR = "tests/out_gary_sa"
os.makedirs(OUTDIR, exist_ok=True)

location_path = initialize_grassdb()
from grass.script import core as gcore       # noqa: E402
import grass.script.array as garray          # noqa: E402

try:
    u = uuid.uuid4().hex
    gcore.parse_command("r.external", input=DEM, band=1, output=f"e{u}",
                         overwrite=True, flags="o")
    gcore.parse_command("g.region", raster=f"e{u}")
    gcore.parse_command("r.fill.dir", input=f"e{u}", output=f"f{u}",
                        direction=f"d{u}", areas=f"a{u}", overwrite=True)
    gcore.parse_command("r.watershed", flags="m", elevation=f"f{u}",
                        accumulation=f"acc{u}", threshold=50, memory=300,
                        overwrite=True)
    gcore.parse_command("r.slope.aspect", elevation=f"e{u}", slope=f"s{u}",
                        format="percent", overwrite=True)
    acc = np.abs(np.nan_to_num(
        np.asarray(garray.array(mapname=f"acc{u}"), dtype=float)))
    slope_pct = np.nan_to_num(
        np.asarray(garray.array(mapname=f"s{u}"), dtype=float))
    region = gcore.region()
    ns_m = float(region["nsres"]) * 111_111
    mean_lat = (float(region["n"]) + float(region["s"])) / 2
    ew_m = float(region["ewres"]) * 111_111 * np.cos(np.deg2rad(mean_lat))
    cell_area_m2 = ns_m * ew_m
finally:
    shutil.rmtree(location_path, ignore_errors=True)

field = _rasterize_field(FIELD, DEM)
ok = field & (acc >= 1) & (slope_pct > 0)
area = acc[ok] * cell_area_m2          # m^2 of contributing area
grad = slope_pct[ok] / 100.0           # rise/run
print(f"\nin-field cells analysed: {ok.sum()}  cell ~{cell_area_m2:.0f} m^2")

# Median slope per log-area bin.
edges = np.logspace(np.log10(area.min()), np.log10(area.max()), 19)
print(f"\n{'area_m2':>10} {'acres':>7} {'n':>5} {'med_slope':>10}  trend")
rows = []
for lo, hi in zip(edges[:-1], edges[1:]):
    m = (area >= lo) & (area < hi)
    if m.sum() >= 8:
        rows.append((np.sqrt(lo * hi), int(m.sum()), float(np.median(grad[m]))))
for i, (a, n, s) in enumerate(rows):
    d = "" if i == 0 else ("UP" if s > rows[i - 1][2] else "down")
    bar = "#" * int(s * 400)
    print(f"{a:10.0f} {a/4046.86:7.2f} {n:5d} {s:10.4f}  {d:4s} {bar}")

peak = max(range(len(rows)), key=lambda i: rows[i][2])
print(f"\nslope peaks at area ~{rows[peak][0]:.0f} m^2 "
      f"(~{rows[peak][0]/4046.86:.1f} ac, ~{rows[peak][0]/cell_area_m2:.0f} cells)")

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    a = np.array([r[0] for r in rows])
    s = np.array([r[2] for r in rows])
    plt.figure(figsize=(7, 5))
    plt.loglog(area, grad, ".", ms=1, alpha=0.15, color="gray")
    plt.loglog(a, s, "o-", color="crimson", label="median per bin")
    plt.axvline(rows[peak][0], ls="--", color="navy", label="slope peak (kink)")
    plt.xlabel("contributing area  (m^2)")
    plt.ylabel("slope  (rise/run)")
    plt.title("Gary's East — slope vs drainage area")
    plt.legend()
    plt.tight_layout()
    out = os.path.join(OUTDIR, "gary_slope_area.png")
    plt.savefig(out, dpi=110)
    print(f"wrote {out}")
except Exception as exc:
    print(f"(no plot: {exc})")
