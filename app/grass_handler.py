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
from osgeo import gdal, ogr, osr


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


# r.watershed D8 `drainage` encoding → (drow, dcol). The row index increases
# southward. 1=NE 2=N 3=NW 4=W 5=SW 6=S 7=SE 8=E (CCW from NE); a negative
# value means the cell drains off-region, 0 means an unresolved depression.
_D8_DELTAS = {
    1: (-1, 1), 2: (-1, 0), 3: (-1, -1), 4: (0, -1),
    5: (1, -1), 6: (1, 0), 7: (1, 1), 8: (0, 1),
}


def _trace_d8_path(drainage, flow_acc, start, channel_threshold, max_steps):
    """
    Walk the r.watershed D8 `drainage` array downslope from `start`
    (row, col), stopping at the first channel cell — one whose absolute
    flow accumulation reaches `channel_threshold`. This is the RUSLE2
    overland-flow transect: divide → point of flow concentration.

    Returns `[(row, col), ...]`, divide → channel head inclusive. Stops
    early on a raster edge, an unresolved depression (direction 0), or a
    loop (should not occur on a depression-filled DEM).
    """
    rows, cols = drainage.shape
    r, c = start
    path = [(r, c)]
    visited = {(r, c)}
    for _ in range(max_steps):
        if len(path) > 1 and abs(int(flow_acc[r, c])) >= channel_threshold:
            break
        delta = _D8_DELTAS.get(abs(int(drainage[r, c])))
        if delta is None:                       # 0 → depression / undetermined
            break
        nr, nc = r + delta[0], c + delta[1]
        if not (0 <= nr < rows and 0 <= nc < cols):
            break
        if (nr, nc) in visited:                 # loop guard
            break
        r, c = nr, nc
        path.append((r, c))
        visited.add((r, c))
    return path


def _path_length_cells(path) -> float:
    """Approximate path length in cell units (diagonal steps = sqrt(2))."""
    total = 0.0
    for (r0, c0), (r1, c1) in zip(path, path[1:]):
        total += 1.41421356 if (r0 != r1 and c0 != c1) else 1.0
    return total


def _divide_mask(drainage):
    """
    Boolean mask of drainage divides — cells that no neighbour drains
    into. Derived from the D8 `drainage` array, so it is robust: it does
    not depend on exact floating-point accumulation values, which MFD
    routing makes fractional.
    """
    absdir = np.abs(drainage).astype(np.int64)
    rows, cols = drainage.shape
    receives = np.zeros(drainage.shape, dtype=bool)
    for code, (dr, dc) in _D8_DELTAS.items():
        # Cells whose drainage == code feed the cell offset by (dr, dc).
        src = absdir == code
        tr = slice(max(0, dr), rows + min(0, dr))
        tc = slice(max(0, dc), cols + min(0, dc))
        sr = slice(max(0, -dr), rows + min(0, -dr))
        sc = slice(max(0, -dc), cols + min(0, -dc))
        receives[tr, tc] |= src[sr, sc]
    return ~receives


def _channel_heads(drainage, channel_mask):
    """
    Boolean mask of channel heads — channel cells (`channel_mask` True)
    that no upstream channel cell drains into. Each is the top of one
    draw; tracing D8 downslope from it gives a continuous draw line.
    """
    absdir = np.abs(drainage).astype(np.int64)
    rows, cols = drainage.shape
    fed_by_channel = np.zeros(drainage.shape, dtype=bool)
    for code, (dr, dc) in _D8_DELTAS.items():
        src = (absdir == code) & channel_mask
        tr = slice(max(0, dr), rows + min(0, dr))
        tc = slice(max(0, dc), cols + min(0, dc))
        sr = slice(max(0, -dr), rows + min(0, -dr))
        sc = slice(max(0, -dc), cols + min(0, -dc))
        fed_by_channel[tr, tc] |= src[sr, sc]
    return channel_mask & ~fed_by_channel


def _rasterize_field(field_src: str, elev_tif: str):
    """
    Rasterize a field-boundary vector onto the DEM grid. Returns a bool
    array (True = inside the field), aligned cell-for-cell with the
    arrays r.watershed produces from `elev_tif`.

    Uses gdal.Rasterize (not RasterizeLayer) so the field is reprojected
    to the DEM's CRS — USGS DEMs are NAD83, field boundaries WGS84.
    """
    ref = gdal.Open(elev_tif)
    mem = gdal.GetDriverByName("MEM").Create(
        "", ref.RasterXSize, ref.RasterYSize, 1, gdal.GDT_Byte,
    )
    mem.SetGeoTransform(ref.GetGeoTransform())
    mem.SetProjection(ref.GetProjection())
    gdal.Rasterize(mem, field_src, burnValues=[1])
    return mem.ReadAsArray().astype(bool)


def get_mfd_flowlines_raw(elev_tif, outdir, options=None, field_shp=None):
    """
    Extract representative RUSLE2 hillslope profiles from `elev_tif`
    (elevation in meters, EPSG:4326). Returns one raw record per kept
    hillslope — caller (geoworker.mfd_flowlines) handles unit conversion,
    DP simplification, shape classification, and per-feature clipping.

    `elev_tif` is the flow-computation DEM — the user-selected
    watershed/analysis polygon's bbox, or the field bounding box + a margin
    when no analysis area was selected — so the upslope contributing area
    is captured. `field_shp`, when given, is the field-boundary vector:
    half-basins are then ranked by their IN-FIELD area, so off-field draws
    in the surrounding margin don't crowd out the field's own hillslopes.
    Without it, ranking falls back to total area.

    A flow line here is the overland-flow transect RUSLE2 needs: it runs
    from a drainage divide (origin of overland flow) DOWN to where flow
    concentrates into a channel. It is NOT the channel itself.

    Pipeline:
      1. r.fill.dir → depression-filled DEM.
      2. r.watershed -m → MFD accumulation, D8 drainage, half-basins.
      3. Half-basins partition the DEM into non-overlapping hillslope
         units; the largest `max_lines` by IN-FIELD area are kept.
      4. Per unit: among its divide cells (cells no neighbour drains into),
         walk the D8 drainage downslope to the first channel cell
         (|accumulation| >= channel_threshold). The longest such
         divide→channel path is the unit's representative hillslope.
      5. Sample the filled DEM along that path → profile_vertices_m.
      6. Draw network: where >= draw_threshold cells concentrate, trace
         the D8 channel from each head down to the outlet — one `draw`
         record per draw (concentrated-flow channels where ephemeral-gully
         erosion happens; complements the sheet-and-rill hillslopes).

    Returns: list of dicts. Hillslope records and draw records share a
    shape, distinguished by `kind`:
        {
            "kind": str,                          # "hillslope" or "draw"
            "rank": int,                          # 1 = largest / longest
            "flow_accumulation_cells": int,       # contributing area
            "line_wkt": str,                      # LineString WKT, EPSG:4326
            "zone_wkt": str,                      # Polygon WKT (draws: EMPTY)
            "profile_vertices_m": [(cum_length_m, elev_m), ...],
        }

    Empty list on flat fields (no cells exceed threshold) — caller posts
    an empty FeatureCollection per spec.
    """
    opts = options or {}
    max_lines = int(opts.get("max_lines", 5))
    min_flow_acc = int(opts.get("min_flow_accumulation_cells", 50))
    # Accumulation at which overland flow is treated as concentrated into a
    # channel — the downslope terminus of a RUSLE2 hillslope. Defaults to the
    # r.watershed stream-definition threshold.
    channel_threshold = int(opts.get("channel_threshold_cells", min_flow_acc))
    # A draw is reported only where enough area concentrates to make it a
    # real channel — higher than the rill-scale channel_threshold.
    draw_threshold = int(opts.get("draw_threshold_cells", 300))
    max_draws = int(opts.get("max_draws", 6))
    # New channel a draw must add over already-kept draws to count as
    # distinct — keeps near-duplicate traces of one draw from piling up.
    draw_min_unique_m = float(opts.get("draw_min_unique_ft", 300)) * 0.3048
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
        drainage_name = f"drainage_{uniq}"
        half_basin_name = f"half_basin_{uniq}"

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
            drainage=drainage_name,
            half_basin=half_basin_name,
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

        # Cell size in metres (DEM resolution is degrees in EPSG:4326).
        mean_lat = (y0 + float(region["s"])) / 2
        ns_res_m = ns_res * 111_111
        ew_res_m = ew_res * 111_111 * max(0.01, np.cos(np.deg2rad(mean_lat)))
        cell_size_m = (ns_res_m + ew_res_m) / 2

        drainage = np.asarray(
            garray.array(mapname=drainage_name), dtype=np.float64,
        )
        drainage = np.nan_to_num(drainage, nan=0.0, posinf=0.0, neginf=0.0)
        half_basin = np.asarray(
            garray.array(mapname=half_basin_name), dtype=np.float64,
        )
        half_basin = np.nan_to_num(half_basin, nan=0.0, posinf=0.0, neginf=0.0)

        divide_all = _divide_mask(drainage)
        max_steps = 4 * (drainage.shape[0] + drainage.shape[1])

        # Half-basins tile the buffered DEM into non-overlapping hillslope
        # units. Rank them by IN-FIELD area, so off-field draws in the
        # buffer margin don't crowd out the field's own hillslopes, then
        # take one representative profile from each of the largest.
        field_mask = None
        if field_shp:
            try:
                fm = _rasterize_field(field_shp, elev_tif)
                if fm.shape == half_basin.shape:
                    field_mask = fm
                else:
                    print(f"field mask {fm.shape} != DEM {half_basin.shape}; "
                          f"ranking half-basins by total area")
            except Exception as exc:
                print(f"field rasterize failed ({exc}); ranking by total area")

        rank_sel = half_basin >= 1
        if field_mask is not None:
            rank_sel &= field_mask
        unit_ids, unit_counts = np.unique(
            half_basin[rank_sel].astype(np.int64), return_counts=True,
        )
        if unit_ids.size == 0:
            return []
        ranked_units = [int(unit_ids[i]) for i in np.argsort(unit_counts)[::-1]]

        results = []
        rank = 0
        for unit_id in ranked_units:
            if rank >= max_lines:
                break
            unit_mask = half_basin == unit_id

            # Representative divide→channel path within this hillslope
            # unit. Score by IN-FIELD length (cells inside `field_mask`),
            # not total length — a path that runs mostly through the buffer
            # margin is not a hillslope of this field.
            ridge_cells = np.argwhere(divide_all & unit_mask)
            if ridge_cells.size == 0:
                continue
            # Cap the candidate walk on very large units.
            if len(ridge_cells) > 3000:
                ridge_cells = ridge_cells[:: len(ridge_cells) // 3000]

            best_path = None
            best_score = -1.0
            for rr, cc in ridge_cells:
                path = _trace_d8_path(
                    drainage, flow_acc, (int(rr), int(cc)),
                    channel_threshold, max_steps,
                )
                if len(path) < 2:
                    continue
                if field_mask is not None:
                    in_field = sum(1 for (r, c) in path if field_mask[r, c])
                    score = in_field * cell_size_m
                else:
                    score = _path_length_cells(path) * cell_size_m
                if score > best_score:
                    best_score = score
                    best_path = path
            if best_path is None or best_score < min_line_length_m:
                continue

            rank += 1
            line_vertices = [
                (x0 + (c + 0.5) * ew_res, y0 - (r + 0.5) * ns_res)
                for (r, c) in best_path
            ]

            # Vectorize this hillslope unit → the zone polygon.
            mask_name = f"hbmask_{uniq}_{rank}"
            zone_vec_name = f"zone_vec_{uniq}_{rank}"
            zone_wkt = "POLYGON EMPTY"
            try:
                gcore.parse_command(
                    "r.mapcalc",
                    expression=(
                        f"{mask_name} = "
                        f"if({half_basin_name} == {unit_id}, 1, null())"
                    ),
                    overwrite=True,
                )
                gcore.parse_command(
                    "r.to.vect", input=mask_name, output=zone_vec_name,
                    type="area", overwrite=True,
                )
                zone_geo_path = os.path.join(
                    outdir, f"{zone_vec_name}.geojson",
                )
                if os.path.exists(zone_geo_path):
                    os.remove(zone_geo_path)
                gcore.parse_command(
                    "v.out.ogr", input=zone_vec_name, output=zone_geo_path,
                    format="GeoJSON", overwrite=True,
                )
                with open(zone_geo_path) as f:
                    zone_wkt = _largest_polygon_wkt(json.load(f))
            except Exception as exc:
                print(f"zone vectorize failed for unit {unit_id}: {exc}")

            profile_m = _build_profile(
                line_vertices, filled_arr, x0, y0, ew_res, ns_res,
            )

            # Contributing area is read at the channel head — the foot of
            # the hillslope, where overland flow concentrates.
            head_r, head_c = best_path[-1]
            head_acc = int(abs(flow_acc[head_r, head_c]))

            results.append({
                "kind": "hillslope",
                "rank": rank,
                "flow_accumulation_cells": head_acc,
                "line_wkt": _linestring_wkt(line_vertices),
                "zone_wkt": zone_wkt,
                "profile_vertices_m": profile_m,
            })

        # ----- draw network: concentrated-flow channels -----------------
        # Where >= draw_threshold cells concentrate, overland flow becomes a
        # draw — the channels where ephemeral-gully erosion happens. Trace
        # each draw as ONE continuous line from its head down to the field
        # outlet (a `draw` record; complements the hillslopes). Lower stems
        # of separate draws overlap where tributaries merge — by design,
        # each draw reads as a whole line.
        channel_mask = np.abs(flow_acc) >= draw_threshold
        traced = []
        for hr, hc in np.argwhere(_channel_heads(drainage, channel_mask)):
            path = _trace_d8_path(
                drainage, flow_acc, (int(hr), int(hc)),
                channel_threshold=10 ** 9, max_steps=max_steps,
            )
            if len(path) >= 2:
                traced.append(path)

        # Many heads trace near-identical paths (they converge fast). Keep
        # the longest, then each next draw only if it adds a real run of
        # NEW channel — yielding the main draw plus distinct tributaries.
        traced.sort(key=len, reverse=True)
        min_unique = max(2, int(draw_min_unique_m / cell_size_m))
        corridor = 3        # cells: draws within this of a kept draw are it
        covered = set()
        draw_paths = []
        for path in traced:
            if sum(cell not in covered for cell in path) >= min_unique:
                draw_paths.append(path)
                # Mark a corridor around the kept draw so a near-parallel
                # trace of the same draw is not also kept.
                for pr, pc in path:
                    for dr in range(-corridor, corridor + 1):
                        for dc in range(-corridor, corridor + 1):
                            covered.add((pr + dr, pc + dc))
            if len(draw_paths) >= max_draws:
                break

        for i, path in enumerate(draw_paths, start=1):
            acc_max = max(int(abs(flow_acc[r, c])) for (r, c) in path)
            verts = [
                (x0 + (c + 0.5) * ew_res, y0 - (r + 0.5) * ns_res)
                for (r, c) in path
            ]
            results.append({
                "kind": "draw",
                "rank": i,
                "flow_accumulation_cells": acc_max,
                "line_wkt": _linestring_wkt(verts),
                "zone_wkt": "POLYGON EMPTY",
                "profile_vertices_m": _build_profile(
                    verts, filled_arr, x0, y0, ew_res, ns_res),
            })

        return results
    finally:
        shutil.rmtree(location_path, ignore_errors=True)
