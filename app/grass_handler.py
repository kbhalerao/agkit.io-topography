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
import os
import shutil
import subprocess
import sys
import uuid


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


def initialize_grassdb():
    """
    Create a fresh GRASS location + PERMANENT mapset under `/tmp/grassdata`,
    initialize the Python session, and return the location path so the
    caller can tear it down on exit.
    """
    myepsg = "4326"
    gisbase = "/usr/local/grass"

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
