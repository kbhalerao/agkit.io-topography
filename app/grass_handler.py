"""
GRASS GIS r.watershed wrapper.

Ported from USGS2021 with one behavior change: L (length_slope) and
LS (slope_steepness) rasters are now exported alongside drainage /
stream / spi / tci.

Each invocation spins up a fresh GRASS location with a random hex
name under `/tmp/grassdata`, links the elevation tif via `r.external`
(no copy), runs `r.watershed`, and writes the selected outputs to
disk as GeoTIFFs. The location is torn down on exit.
"""
import binascii
import json
import os
import shutil
import subprocess
import sys
import uuid

import numpy as np
from osgeo import ogr, osr


# Outputs from r.watershed that we always export as rasters.
# The handler iterates this and runs r.out.gdal for each.
RASTER_OUTPUTS = (
    "drainage",
    "stream",
    "spi",
    "tci",
    "length_slope",      # NEW — L
    "slope_steepness",   # NEW — LS
)


def _resolve_gisbase() -> str:
    """
    Resolve the GRASS install root.

    Order of precedence:
      1. ``GISBASE`` env var (explicit; set by the Dockerfile to
         ``/usr/local/grass`` for the Alpine image).
      2. ``grass --config path`` output (covers Ubuntu apt grass at
         ``/usr/lib/grass8x``).
      3. ``/usr/local/grass`` as a last-resort fallback.
    """
    env_gisbase = os.environ.get("GISBASE")
    if env_gisbase and os.path.isdir(env_gisbase):
        return env_gisbase
    try:
        result = subprocess.run(
            ["grass", "--config", "path"],
            check=True, capture_output=True, text=True, timeout=10,
        )
        path = result.stdout.strip()
        if path and os.path.isdir(path):
            return path
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    return "/usr/local/grass"


def initialize_grassdb():
    """
    Create a fresh GRASS location + PERMANENT mapset under `/tmp/grassdata`,
    initialize the Python session, and return the location path so the
    caller can tear it down on exit.
    """
    myepsg = "4326"
    gisbase = _resolve_gisbase()

    os.environ["GISBASE"] = gisbase
    gpydir = os.path.join(gisbase, "etc", "python")
    sys.path.append(gpydir)

    gisdb = "/tmp/grassdata"
    os.makedirs(gisdb, exist_ok=True)

    string_length = 16
    location = binascii.hexlify(os.urandom(string_length)).decode()
    mapset = "PERMANENT"
    location_path = os.path.join(gisdb, location)

    startcmd = f"grass -c epsg:{myepsg} -e {location_path}"
    print(startcmd)
    subprocess.run(startcmd, shell=True, check=True, capture_output=True)

    os.environ["GISDBASE"] = gisdb
    path = os.getenv("LD_LIBRARY_PATH")
    lib_dir = os.path.join(gisbase, "lib")
    os.environ["LD_LIBRARY_PATH"] = (
        lib_dir + os.pathsep + path if path else lib_dir
    )
    os.environ["LANG"] = "en_US"
    os.environ["LOCALE"] = "C"

    import grass.script.setup as gsetup
    gsetup.init(gisdb, location, mapset)
    return location_path


def get_contour_lines(elev_tif: str, outdir: str, step_ft: float = 2) -> str:
    """
    Run r.contour against `elev_tif` (elevation in meters) and write a
    GeoJSON of contour lines into `outdir`.

    Internally converts the DEM to feet via r.mapcalc so the contour
    `level` attribute is in feet (RUSLE2 / agronomic convention).

    Returns the GeoJSON path. The caller is responsible for renaming
    `level` → `level_ft` and adding the `is_index` tag — that's a
    string-rename, not a GRASS concern.
    """
    location_path = initialize_grassdb()
    from grass.script import core as gcore
    try:
        uniq = uuid.uuid4().hex
        outpath = os.path.join(outdir, f"contours_{uniq}.geojson")

        gcore.parse_command(
            "r.external",
            input=elev_tif, band=1,
            output=f"elev_m_{uniq}",
            overwrite=True, flags="o",
        )
        gcore.parse_command("g.region", raster=f"elev_m_{uniq}")

        gcore.parse_command(
            "r.mapcalc",
            expression=f"elev_ft_{uniq} = elev_m_{uniq} * 3.28084",
            overwrite=True,
        )

        gcore.parse_command(
            "r.contour",
            input=f"elev_ft_{uniq}",
            output=f"contours_{uniq}",
            step=step_ft,
            overwrite=True,
        )

        gcore.parse_command(
            "v.out.ogr",
            input=f"contours_{uniq}",
            output=outpath,
            format="GeoJSON",
            overwrite=True,
        )
        return outpath
    finally:
        shutil.rmtree(location_path, ignore_errors=True)


def get_watershed_maps(elev_tif: str, outdir: str) -> dict:
    """
    Run r.watershed against `elev_tif` and write the configured
    raster outputs into `outdir`. Returns a dict mapping output name
    (e.g. ``"length_slope"``) → tif path.

    Always exports drainage, stream, spi, tci, length_slope (L), and
    slope_steepness (LS). Callers that only need a subset can index
    into the returned dict.
    """
    location_path = initialize_grassdb()
    from grass.script import core as gcore
    try:
        _basename, ext = os.path.splitext(os.path.basename(elev_tif))
        uniq = uuid.uuid4().hex
        results: dict[str, str] = {}

        gcore.parse_command(
            "r.external",
            input=elev_tif, band=1,
            output=f"elev{uniq}",
            overwrite=True, flags="o",
        )
        gcore.parse_command("g.region", raster=f"elev{uniq}")
        gcore.parse_command(
            "r.watershed",
            flags="b",
            elevation=f"elev{uniq}",
            threshold=30,
            convergence=5,
            memory=300,
            drainage=f"drainage{uniq}",
            accumulation=f"accumulation{uniq}",
            basin=f"basin{uniq}",
            stream=f"stream{uniq}",
            half_basin=f"half_basin{uniq}",
            length_slope=f"length_slope{uniq}",
            slope_steepness=f"slope_steepness{uniq}",
            tci=f"tci{uniq}",
            spi=f"spi{uniq}",
            overwrite=True,
        )

        for output in RASTER_OUTPUTS:
            outtif = f"{outdir}/{output}{ext}"
            gcore.parse_command("g.region", raster=f"{output}{uniq}")
            gcore.parse_command(
                "r.out.gdal",
                flags="tmc",
                input=f"{output}{uniq}",
                output=outtif,
                format="GTiff",
                overwrite=True,
                type="Float64",
                nodata=-999,
            )
            results[output] = outtif
        return results
    finally:
        shutil.rmtree(location_path, ignore_errors=True)


# ---------------------------------------------------------------------------
# MFD flow-line extraction
# ---------------------------------------------------------------------------

_LON_LAT_PROJ4 = "+proj=longlat +ellps=WGS84 +datum=WGS84 +no_defs"
_ALBERS_PROJ4 = (
    "+proj=aea +lat_1=29.5 +lat_2=45.5 +lat_0=23 +lon_0=-96 "
    "+x_0=0 +y_0=0 +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs"
)


def _pick_seeds(flow_acc: np.ndarray, max_lines: int, min_flow_acc: int,
                nms_radius_cells: int) -> list:
    """
    Top-N flow-accumulation cells with non-max suppression.

    Returns `[(row, col, value), ...]` in descending value order, no two
    seeds within `nms_radius_cells` Chebyshev distance.

    Pure numpy, side-effect free — independently testable.
    """
    candidate_pool = max(max_lines * 4, max_lines)
    flat_idx = np.argsort(flow_acc, axis=None)[::-1]
    rows, cols = np.unravel_index(flat_idx, flow_acc.shape)

    kept = []
    inspected = 0
    for r, c in zip(rows, cols):
        v = flow_acc[r, c]
        if v < min_flow_acc:
            break
        inspected += 1
        too_close = any(
            max(abs(int(r) - kr), abs(int(c) - kc)) <= nms_radius_cells
            for kr, kc, _ in kept
        )
        if not too_close:
            kept.append((int(r), int(c), float(v)))
            if len(kept) >= max_lines:
                break
        if inspected >= candidate_pool and len(kept) >= 1:
            break
    return kept


def _extract_single_linestring(geojson: dict):
    """
    Return `[(x, y), ...]` for the single LineString in `geojson`, or None
    if the FeatureCollection contains a MultiLineString (no-data hole — drop
    per spec) or no LineString at all.
    """
    features = geojson.get("features", [])
    if not features:
        return None
    coords = []
    for feat in features:
        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        if gtype == "LineString":
            coords.extend(geom["coordinates"])
        elif gtype == "MultiLineString":
            return None
    if len(coords) < 2:
        return None
    return [(float(c[0]), float(c[1])) for c in coords]


def _largest_polygon_wkt(geojson: dict) -> str:
    """Return the WKT of the largest Polygon/MultiPolygon in a FeatureCollection."""
    best = None
    best_area = -1.0
    for feat in geojson.get("features", []):
        geom = feat.get("geometry") or {}
        if geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        ogeom = ogr.CreateGeometryFromJson(json.dumps(geom))
        area = ogeom.GetArea()
        if area > best_area:
            best = ogeom
            best_area = area
    if best is None:
        return "POLYGON EMPTY"
    return best.ExportToWkt()


def _linestring_wkt(vertices) -> str:
    coord_str = ", ".join(f"{x} {y}" for x, y in vertices)
    return f"LINESTRING ({coord_str})"


def _build_profile(line_vertices, filled_arr, x0, y0, ew_res, ns_res) -> list:
    """
    Sample the (filled) DEM at each line vertex and build the
    `(cum_length_m, elev_m)` sequence. Distances are computed in EPSG:5072
    (Albers, meters) — line vertices are in EPSG:4326.
    """
    if not line_vertices:
        return []

    src_srs = osr.SpatialReference()
    src_srs.ImportFromProj4(_LON_LAT_PROJ4)
    planar_srs = osr.SpatialReference()
    planar_srs.ImportFromProj4(_ALBERS_PROJ4)
    to_planar = osr.CoordinateTransformation(src_srs, planar_srs)

    profile = []
    cum_length_m = 0.0
    prev_planar = None
    rows, cols = filled_arr.shape

    for (x, y) in line_vertices:
        col = int((x - x0) / ew_res)
        row = int((y0 - y) / ns_res)
        if not (0 <= row < rows and 0 <= col < cols):
            continue
        elev_m = float(filled_arr[row, col])
        if not np.isfinite(elev_m):
            continue

        px, py, _ = to_planar.TransformPoint(x, y)
        if prev_planar is not None:
            dx = px - prev_planar[0]
            dy = py - prev_planar[1]
            cum_length_m += (dx * dx + dy * dy) ** 0.5
        prev_planar = (px, py)

        profile.append((cum_length_m, elev_m))
    return profile


def get_mfd_flowlines_raw(elev_tif: str, outdir: str, options=None) -> list:
    """
    Run the MFD flow-line extraction pipeline against `elev_tif`
    (elevation in meters, EPSG:4326). Returns one raw record per kept
    flow line — caller (geoworker.mfd_flowlines) handles unit conversion,
    DP simplification, shape classification, and per-feature clipping.

    Pipeline:
      1. r.fill.dir → filled DEM + D8 direction (byproduct).
      2. r.watershed -m → MFD accumulation.
      3. Top-N seed selection on the accumulation array (numpy NMS).
      4. r.drain per seed → one LineString per seed (D8-routed).
      5. r.water.outlet at each line's outlet → D8 catchment polygon.
      6. Sample filled DEM at each line vertex → profile_vertices_m.

    Returns: list of dicts, each:
        {
            "rank": int,                          # 1 = highest-acc outlet
            "flow_accumulation_cells": int,
            "line_wkt": str,                      # LineString WKT, EPSG:4326
            "zone_wkt": str,                      # Polygon WKT, EPSG:4326
            "profile_vertices_m": [(cum_length_m, elev_m), ...],
        }

    Empty list on flat fields (no cells exceed threshold) — caller posts
    an empty FeatureCollection per spec.
    """
    opts = options or {}
    max_lines = int(opts.get("max_lines", 5))
    min_flow_acc = int(opts.get("min_flow_accumulation_cells", 50))
    min_line_length_m = float(opts.get("min_line_length_ft", 100)) * 0.3048

    location_path = initialize_grassdb()
    from grass.script import core as gcore
    import grass.script.array as garray

    try:
        uniq = uuid.uuid4().hex
        elev_name = f"elev_{uniq}"
        filled_name = f"filled_{uniq}"
        d8_name = f"d8_{uniq}"
        areas_name = f"fill_areas_{uniq}"
        flow_acc_name = f"flow_acc_{uniq}"

        gcore.parse_command(
            "r.external", input=elev_tif, band=1,
            output=elev_name, overwrite=True, flags="o",
        )
        gcore.parse_command("g.region", raster=elev_name)

        gcore.parse_command(
            "r.fill.dir",
            input=elev_name,
            output=filled_name,
            direction=d8_name,
            areas=areas_name,
            overwrite=True,
        )

        gcore.parse_command(
            "r.watershed",
            flags="m",
            elevation=filled_name,
            accumulation=flow_acc_name,
            threshold=min_flow_acc,
            memory=300,
            overwrite=True,
        )

        flow_acc = np.asarray(garray.array(mapname=flow_acc_name), dtype=np.float64)
        flow_acc = np.nan_to_num(flow_acc, nan=0.0, posinf=0.0, neginf=0.0)
        filled_arr = np.asarray(garray.array(mapname=filled_name), dtype=np.float64)

        region = gcore.region()
        x0 = float(region["w"])
        y0 = float(region["n"])
        ew_res = float(region["ewres"])
        ns_res = float(region["nsres"])

        # NMS radius in cells. The DEM resolution is in degrees in EPSG:4326 —
        # convert min_line_length_m to degrees at the region's latitude.
        # 1 deg lat ~= 111_111 m; lon scaled by cos(lat).
        mean_lat = (y0 + float(region["s"])) / 2
        ns_res_m = ns_res * 111_111
        ew_res_m = ew_res * 111_111 * max(0.01, np.cos(np.deg2rad(mean_lat)))
        cell_size_m = (ns_res_m + ew_res_m) / 2
        nms_radius_cells = max(1, int((min_line_length_m / 2) / cell_size_m))

        seeds = _pick_seeds(
            flow_acc, max_lines, min_flow_acc, nms_radius_cells,
        )
        if not seeds:
            return []

        results = []
        for rank, (row, col, acc_val) in enumerate(seeds, start=1):
            seed_x = x0 + (col + 0.5) * ew_res
            seed_y = y0 - (row + 0.5) * ns_res

            line_name = f"line_{uniq}_{rank}"
            try:
                gcore.parse_command(
                    "r.drain",
                    input=filled_name,
                    direction=d8_name,
                    start_coordinates=f"{seed_x},{seed_y}",
                    output=line_name,
                    overwrite=True,
                )
            except Exception as exc:
                print(f"r.drain failed for seed {rank}: {exc}")
                continue

            line_vec_name = f"line_vec_{uniq}_{rank}"
            try:
                gcore.parse_command(
                    "r.thin",
                    input=line_name,
                    output=f"{line_name}_thin",
                    overwrite=True,
                )
                gcore.parse_command(
                    "r.to.vect",
                    input=f"{line_name}_thin",
                    output=line_vec_name,
                    type="line",
                    overwrite=True,
                )
            except Exception as exc:
                print(f"line vectorize failed for seed {rank}: {exc}")
                continue

            line_geo_path = os.path.join(outdir, f"{line_vec_name}.geojson")
            if os.path.exists(line_geo_path):
                os.remove(line_geo_path)
            try:
                gcore.parse_command(
                    "v.out.ogr",
                    input=line_vec_name,
                    output=line_geo_path,
                    format="GeoJSON",
                    overwrite=True,
                )
            except Exception as exc:
                print(f"line export failed for seed {rank}: {exc}")
                continue

            with open(line_geo_path) as f:
                line_geo = json.load(f)
            line_vertices = _extract_single_linestring(line_geo)
            if line_vertices is None:
                print(f"line {rank} dropped: not a single LineString")
                continue
            if len(line_vertices) < 2:
                continue

            outlet_x, outlet_y = line_vertices[-1]
            catch_name = f"catch_{uniq}_{rank}"
            try:
                gcore.parse_command(
                    "r.water.outlet",
                    input=d8_name,
                    output=catch_name,
                    coordinates=f"{outlet_x},{outlet_y}",
                    overwrite=True,
                )
            except Exception as exc:
                print(f"r.water.outlet failed for line {rank}: {exc}")
                continue

            catch_vec_name = f"catch_vec_{uniq}_{rank}"
            try:
                gcore.parse_command(
                    "r.to.vect",
                    input=catch_name,
                    output=catch_vec_name,
                    type="area",
                    overwrite=True,
                )
            except Exception as exc:
                print(f"catchment vectorize failed for line {rank}: {exc}")
                continue

            catch_geo_path = os.path.join(outdir, f"{catch_vec_name}.geojson")
            if os.path.exists(catch_geo_path):
                os.remove(catch_geo_path)
            try:
                gcore.parse_command(
                    "v.out.ogr",
                    input=catch_vec_name,
                    output=catch_geo_path,
                    format="GeoJSON",
                    overwrite=True,
                )
            except Exception as exc:
                print(f"catchment export failed for line {rank}: {exc}")
                continue

            with open(catch_geo_path) as f:
                catch_geo = json.load(f)
            zone_wkt = _largest_polygon_wkt(catch_geo)

            profile_m = _build_profile(
                line_vertices, filled_arr, x0, y0, ew_res, ns_res,
            )

            results.append({
                "rank": rank,
                "flow_accumulation_cells": int(acc_val),
                "line_wkt": _linestring_wkt(line_vertices),
                "zone_wkt": zone_wkt,
                "profile_vertices_m": profile_m,
            })

        return results
    finally:
        shutil.rmtree(location_path, ignore_errors=True)
