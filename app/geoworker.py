"""
LambdaGISProcessor — the core geo worker.

Ported from USGS2021. Two structural changes:

1. **No Django token auth.** `handle_posting()` and the new scalar paths
   call `app.poster` directly, which uses the per-artifact signed
   `post_url` minted by Django.
2. **L / LS exports**. New functions:
   * `watershed_length_slope_raster` — exports the L raster.
   * `watershed_slope_steepness_raster` — exports the LS raster.
   * `watershed_length_slope` — reduces L over the field polygon and
     posts the scalar to `event['post']['scalar_url']`.
   * `watershed_slope_steepness` — same for LS.

All named functions remain methods on the class; the handler dispatches
by name from `event['metadata']['function_name']`.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from shutil import copyfile
from tempfile import NamedTemporaryFile

import numpy as np
from osgeo import gdal, gdalconst, ogr, osr

from app import settings
from app.geocolorize import GeoColorize
from app.grass_handler import (
    get_contour_lines,
    get_mfd_flowlines_raw,
    get_watershed_maps,
)
from app.poster import post_raster, post_scalar
from app.reducers import reduce_length_slope, reduce_slope_steepness
from app.ziphandler import download_and_unzip, handle_USGS_DEM
from app.downloader import get_path, s3_client


gdal.UseExceptions()


def _cleanup(file_paths) -> None:
    """Best-effort `rm -rf`. Used between jobs to keep /tmp under the
    Lambda 512MB ephemeral storage cap."""
    try:
        if not isinstance(file_paths, list):
            file_paths = [file_paths]
        for fp in file_paths:
            subprocess.call(
                f"rm -rf {fp}", shell=True,
                stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            )
    except Exception:
        pass


def _raster_bounds_4326(path):
    """Return a raster's bounds as ``[west, south, east, north]`` in
    EPSG:4326.

    PNG postback artifacts carry no georeferencing, so the bounds travel
    alongside the file (see `poster.post_raster`). The posted rasters are
    already geographic, but corners are reprojected defensively if not.
    """
    ds = gdal.Open(path)
    try:
        gt = ds.GetGeoTransform()
        nx, ny = ds.RasterXSize, ds.RasterYSize
        xs = [gt[0], gt[0] + nx * gt[1] + ny * gt[2]]
        ys = [gt[3], gt[3] + nx * gt[4] + ny * gt[5]]
        src_wkt = ds.GetProjection()
    finally:
        ds = None

    src_srs = osr.SpatialReference()
    if src_wkt:
        src_srs.ImportFromWkt(src_wkt)
    dst_srs = osr.SpatialReference()
    dst_srs.ImportFromEPSG(4326)

    if src_wkt and not src_srs.IsSame(dst_srs):
        src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        dst_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        ct = osr.CoordinateTransformation(src_srs, dst_srs)
        pts = [ct.TransformPoint(x, y)[:2] for x in xs for y in ys]
        lons = [p[0] for p in pts]
        lats = [p[1] for p in pts]
        return [min(lons), min(lats), max(lons), max(lats)]
    return [min(xs), min(ys), max(xs), max(ys)]


class LambdaGISProcessor:
    """
    Job dispatcher with one method per named `function_name`.

    Layout note: the `_s3` client is only used by the rasterize_and_colorize
    path (which still pulls a zip from S3). Everything else is either
    Lambda-local or hits the public USGS bucket via `app.ziphandler`.
    """

    # Fallback DEM domain when no analysis area is selected: the field
    # bbox padded by this many degrees (~220 m) — the pre-watershed
    # behavior. See docs/watershed-bounded-flow-feature.md.
    _FALLBACK_BUFFER_DEG = 0.002

    def __init__(self):
        self.IN_TEST = settings.IN_TEST
        self.cleanup = False
        self.tmpdirname = settings.TEMP_FILE_PATH.rstrip("/") + "/"
        self.cache: dict = {}
        self.shp = None
        self.layer = None
        self.xPixel = 0.00005
        self.yPixel = 0.00005

    # ----- helpers ------------------------------------------------------

    @staticmethod
    def _round_higher(str_val):
        return int(math.ceil(abs(float(str_val))))

    @staticmethod
    def _round_lower(str_val):
        return int(math.floor(abs(float(str_val))))

    def _file_downloader(self, bucket, key, download_path):
        if self.IN_TEST:
            copyfile(os.path.join("/tests/", key), download_path)
            return
        s3_client().download_file(bucket, key, download_path)

    # ----- raster ops (carried over from USGS2021) ---------------------

    def combine_rasters(self, rasters):
        """Merge multiple DEM tiles into a single raster."""
        rasters = [r for r in rasters if os.path.exists(r)]
        print("Rasters to combine:  ", rasters)
        with NamedTemporaryFile(suffix=".tif", delete=False) as outfile:
            gdal.Warp(
                outfile.name, rasters, format="GTiff",
                outputType=gdalconst.GDT_Float32,
                srcAlpha=True, dstAlpha=True,
                srcNodata=0, dstNodata=0,
                options="-r near",
            )
        return outfile.name

    def get_geom_from_field_geojson_file(self, field_geojson):
        geom_txt = json.dumps(field_geojson["features"][0]["geometry"])
        return ogr.CreateGeometryFromJson(geom_txt)

    def open_shapefile(self, shp):
        in_driver = ogr.GetDriverByName("ESRI Shapefile")
        self.shp = in_driver.Open(shp, 0)

    def get_shapefile_extent(self, shp, close=True):
        self.open_shapefile(shp)
        in_layer = self.shp.GetLayer()
        extent = in_layer.GetExtent()
        # (xmin, xmax, ymin, ymax) → (xmin, ymin, xmax, ymax)
        return (extent[0], extent[2], extent[1], extent[3])

    def set_layer(self):
        if self.shp is not None and self.layer is None:
            self.layer = self.shp.GetLayer()

    def get_field_extent(self, field_geojson):
        geom = self.get_geom_from_field_geojson_file(field_geojson)
        env = geom.GetEnvelope()
        return (env[0], env[2], env[1], env[3])

    # ----- analysis-extent helpers (watershed-bounded flow) -------------

    @staticmethod
    def _buffer_extent(extent, buffer_deg):
        """Pad an ``(xmin, ymin, xmax, ymax)`` bbox by `buffer_deg` on all
        sides."""
        return (
            extent[0] - buffer_deg, extent[1] - buffer_deg,
            extent[2] + buffer_deg, extent[3] + buffer_deg,
        )

    @staticmethod
    def _union_extent(a, b):
        """Smallest ``(xmin, ymin, xmax, ymax)`` bbox enclosing both inputs."""
        return (
            min(a[0], b[0]), min(a[1], b[1]),
            max(a[2], b[2]), max(a[3], b[3]),
        )

    @staticmethod
    def _watershed_extent(watershed_boundary):
        """Union bbox ``(xmin, ymin, xmax, ymax)`` of a `watershed_boundary`
        FeatureCollection, or ``None`` when it carries no usable polygon.

        `watershed_boundary` is the user-selected analysis area (see
        `docs/watershed-bounded-flow-feature.md`). Accepts the dict form or
        a JSON string — mirroring how `field_boundary` may arrive — and
        treats null / empty / geometry-less input as "no selection", which
        routes the caller to the field-buffered fallback domain.
        """
        if not watershed_boundary:
            return None
        if isinstance(watershed_boundary, str):
            try:
                watershed_boundary = json.loads(watershed_boundary)
            except (ValueError, TypeError):
                return None
        features = (watershed_boundary or {}).get("features") or []
        xmin = ymin = float("inf")
        xmax = ymax = float("-inf")
        found = False
        for feat in features:
            geom = (feat or {}).get("geometry")
            if not geom:
                continue
            ogeom = ogr.CreateGeometryFromJson(json.dumps(geom))
            if ogeom is None or ogeom.IsEmpty():
                continue
            gx0, gx1, gy0, gy1 = ogeom.GetEnvelope()
            xmin, xmax = min(xmin, gx0), max(xmax, gx1)
            ymin, ymax = min(ymin, gy0), max(ymax, gy1)
            found = True
        return (xmin, ymin, xmax, ymax) if found else None

    def get_dem(self, dem_tile_lat, dem_tile_long, clip_extent=None, download_folder="dem/"):
        """Retrieve a single DEM raster by lat/long of the NW corner.

        `clip_extent`, when given, is the ``(xmin, ymin, xmax, ymax)`` bbox
        the tile is clipped to on download (keeps `/tmp` small)."""
        dem_tile_long = ("0" + str(dem_tile_long))[-3:]
        demfn = f"USGS_13_n{dem_tile_lat}w{dem_tile_long}.tif"

        dem_folder = os.path.join(self.tmpdirname, download_folder)
        dem_path = os.path.join(dem_folder, demfn)

        if os.path.exists(dem_path):
            return dem_path
        if not os.path.exists(dem_folder):
            os.makedirs(dem_folder, exist_ok=True)

        if self.IN_TEST:
            return f"tests/{demfn}"

        try:
            print("DEM lambda download path:  ", dem_path)
            dem_path = handle_USGS_DEM(dem_folder, dem_tile_lat, dem_tile_long, clip_extent)
            if dem_path:
                return dem_path
        except Exception as exc:
            print(f"DEM fetch failed for n{dem_tile_lat}w{dem_tile_long}: {exc}")
        return None

    def get_dem_raster_set(self, extent):
        """List of DEM tiles intersecting `extent` — an
        ``(xmin, ymin, xmax, ymax)`` bbox in EPSG:4326. Each tile is
        clipped-on-download to `extent`."""
        long_west, lat_south, long_east, lat_north = (
            self._round_higher(extent[0]),
            self._round_higher(extent[1]),
            self._round_higher(extent[2]),
            self._round_higher(extent[3]),
        )
        dems = []
        lat_range = range(lat_south, lat_north + 1) if lat_north - lat_south > 0 else [lat_north]
        long_range = range(long_east, long_west + 1) if long_west - long_east > 0 else [long_west]
        for lat in lat_range:
            for lng in long_range:
                dem = self.get_dem(lat, lng, extent)
                if dem:
                    dems.append(dem)
        return dems

    def get_elev_raster(self, extent):
        """Merged DEM covering `extent` ``(xmin, ymin, xmax, ymax)``,
        EPSG:4326."""
        dem_rasters = self.get_dem_raster_set(extent)
        if not dem_rasters:
            raise Exception("No DEM rasters intersect the analysis extent")
        if len(dem_rasters) > 1:
            return self.combine_rasters(dem_rasters)
        return dem_rasters[0]

    def slope(self, src, xyunit="deg", zunit="m"):
        """gdaldem slope as percent. Scale depends on x/y units."""
        if xyunit == "deg":
            scale = 111120 if zunit == "m" else (370400 if zunit == "ft" else 1)
        elif xyunit == "m":
            scale = 1 if zunit in ("m", None) else (0.01 if zunit == "cm" else 1)
        elif xyunit == "ft":
            scale = 1 if zunit in ("ft", None) else 1
        else:
            raise NotImplementedError("Units given to compute slope are not implemented")

        src_ds = gdal.Open(src)
        try:
            gdal.DEMProcessing(
                destName=f"{src}_slp.tif", srcDS=src_ds, processing="slope",
                scale=scale, format="GTiff", slopeFormat="percent",
            )
        finally:
            src_ds = None
        return f"{src}_slp.tif"

    def get_shapefile_from_geojson_ogr(self, field_geojson, with_buffer=False, buff_dist=None):
        """Materialize the field boundary as an ESRI Shapefile for clipping."""
        driver = ogr.GetDriverByName("ESRI Shapefile")
        out_shapefile = os.path.join(self.tmpdirname, "field_boundary.shp")
        if os.path.exists(out_shapefile):
            driver.DeleteDataSource(out_shapefile)
        shape_data = driver.CreateDataSource(out_shapefile)

        spatial_reference = osr.SpatialReference()
        res = spatial_reference.ImportFromProj4(
            "+proj=longlat +ellps=WGS84 +datum=WGS84 +no_defs",
        )
        if res != 0:
            raise RuntimeError(f"{res}: could not import EPSG:4326")

        geom = self.get_geom_from_field_geojson_file(field_geojson)
        geom_type = ogr.wkbMultiPolygon if geom.GetGeometryName() == "MultiPolygon" else ogr.wkbPolygon
        layer = shape_data.CreateLayer(out_shapefile, srs=spatial_reference, geom_type=geom_type)
        layer_defn = layer.GetLayerDefn()

        feature = ogr.Feature(layer_defn)
        if with_buffer:
            feature.SetGeometry(geom.Buffer(buff_dist or 0.001))
        else:
            feature.SetGeometry(geom)
        feature.SetFID(0)
        layer.CreateFeature(feature)

        layer = None
        feature = None
        shape_data.Destroy()
        return out_shapefile

    def gdal_clip_to_bounds(self, dem_raster, extent):
        """Clip `dem_raster` to an explicit ``(xmin, ymin, xmax, ymax)``
        bbox in EPSG:4326.

        `gdal.Warp` pads with nodata where the bbox extends past the
        source raster — harmless here: the result is always re-clipped to
        the field (or field + 50 ft) before postback."""
        with NamedTemporaryFile(suffix=".tif", delete=False) as temp:
            gdal.Warp(
                temp.name, dem_raster,
                srcNodata=0, dstNodata=0,
                outputBounds=list(extent), format="GTiff",
            )
        return temp.name

    def gdal_translate_elev(self, dem_raster):
        """Project EPSG:4326 → EPSG:5072 (NAD83 Albers, meters)."""
        src_srs = osr.SpatialReference()
        if src_srs.ImportFromProj4("+proj=longlat +ellps=WGS84 +datum=WGS84 +no_defs") != 0:
            raise RuntimeError("could not import EPSG:4326")
        dst_srs = osr.SpatialReference()
        if dst_srs.ImportFromProj4(
            "+proj=aea +lat_1=29.5 +lat_2=45.5 +lat_0=23 +lon_0=-96 +x_0=0 +y_0=0 "
            "+ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs",
        ) != 0:
            raise RuntimeError("could not import EPSG:5072")

        with NamedTemporaryFile(suffix=".tif", delete=False) as temp:
            gdal.Warp(
                temp.name, dem_raster,
                srcSRS=src_srs, dstSRS=dst_srs,
                srcNodata=0, dstNodata=0, format="GTiff",
            )
        return temp.name

    def gdal_warp_to_geo(self, raster):
        """Warp `raster` back to EPSG:4326 (geographic)."""
        ds = gdal.Open(raster)
        try:
            src_srs = osr.SpatialReference(wkt=ds.GetProjection())
        finally:
            ds = None
        dst_srs = osr.SpatialReference()
        if dst_srs.ImportFromProj4("+proj=longlat +ellps=WGS84 +datum=WGS84 +no_defs") != 0:
            raise RuntimeError("could not import EPSG:4326")
        with NamedTemporaryFile(suffix=".tif", delete=False) as temp:
            gdal.Warp(
                temp.name, raster,
                srcNodata=0, dstNodata=0,
                format="GTiff",
                srcSRS=src_srs, dstSRS=dst_srs,
                copyMetadata=True,
            )
        return temp.name

    def gdal_warp_clip(self, raster_to_clip, cutline_shapefile):
        """Clip raster to a shapefile cutline."""
        ds = gdal.Open(raster_to_clip)
        try:
            src_no_data = ds.GetRasterBand(1).GetNoDataValue()
        except Exception:
            src_no_data = 0
        try:
            src_srs = osr.SpatialReference(wkt=ds.GetProjection())
        finally:
            ds = None
        dst_srs = osr.SpatialReference()
        if dst_srs.ImportFromProj4("+proj=longlat +ellps=WGS84 +datum=WGS84 +no_defs") != 0:
            raise RuntimeError("could not import EPSG:4326")
        dst_no_data = src_no_data if src_no_data is not None else 0
        with NamedTemporaryFile(suffix=".tif", delete=False) as temp:
            gdal.Warp(
                temp.name, raster_to_clip,
                srcNodata=src_no_data, dstNodata=dst_no_data,
                format="GTiff",
                srcSRS=src_srs, dstSRS=dst_srs,
                cropToCutline=True, cutlineDSName=cutline_shapefile,
                copyMetadata=True,
            )
        return temp.name

    # ----- common cache -------------------------------------------------

    def build_common_cache(self, payload):
        """Field shapefile + DEM rasters shared across every job in a batch.

        Two clipped DEMs are produced:

        * ``clipped_raster`` — field bbox + ~220 m buffer
          (``_FALLBACK_BUFFER_DEG``). The domain for contours, slope, and
          the fallback flow domain.
        * ``watershed_raster`` — the flow-computation domain. With a
          ``watershed_boundary`` in the metadata (the user-selected
          analysis polygon), this is that polygon's union bbox; without
          one it aliases ``clipped_raster``.

        Both are clipped from a single merged DEM fetched at the union of
        the two extents, so the on-disk tile cache serves both clips
        consistently. See docs/watershed-bounded-flow-feature.md.
        """
        if self.cache:
            return
        meta = payload["metadata"]
        field_shp = self.get_shapefile_from_geojson_ogr(meta["field_boundary"])

        field_extent = self.get_field_extent(meta["field_boundary"])
        buffered_extent = self._buffer_extent(
            field_extent, self._FALLBACK_BUFFER_DEG,
        )
        ws_extent = self._watershed_extent(meta.get("watershed_boundary"))
        flow_extent = ws_extent if ws_extent is not None else buffered_extent

        dem_raster = self.get_elev_raster(
            self._union_extent(buffered_extent, flow_extent),
        )
        self.cache["field_shp"] = field_shp
        self.cache["dem_raster"] = dem_raster
        self.cache["clipped_raster"] = self.gdal_clip_to_bounds(
            dem_raster, buffered_extent,
        )
        if ws_extent is None:
            # No analysis area selected — flow jobs fall back to the
            # field-buffered domain (the pre-watershed behavior).
            self.cache["watershed_raster"] = self.cache["clipped_raster"]
            self.cache["flow_domain"] = "field_buffer"
        else:
            self.cache["watershed_raster"] = self.gdal_clip_to_bounds(
                dem_raster, flow_extent,
            )
            self.cache["flow_domain"] = "watershed"

    def watershed_common(self, payload):
        self.build_common_cache(payload)
        if "watershed" in self.cache:
            return
        os.makedirs("/tmp/watershed", exist_ok=True)
        self.cache["watershed"] = get_watershed_maps(
            self.cache["watershed_raster"], "/tmp/watershed",
        )

    # ----- posting glue -------------------------------------------------

    def handle_posting(self, new_file, color_map, payload):
        """
        Post every output spec in `payload['post']['output']` to its
        signed `post_url`. Raster outputs go as multipart; png is
        derived from the tif via colorize.
        """
        # `new_file` is the georeferenced source tif; the png is colorized
        # from it, so both share these bounds. Sent on every postback so
        # Django can place the (georef-less) png at its true extent.
        extent = _raster_bounds_4326(new_file)
        responses = []
        for output in payload["post"]["output"]:
            ext = (output.get("extension") or "").lower()
            if ext == "tif":
                outfile = new_file
                label = "TIF"
            elif ext == "png":
                outfile = GeoColorize(folder=self.tmpdirname.rstrip("/")).colorize(new_file, color_map)
                label = "PNG"
            else:
                raise ValueError(f"Unknown output extension: {ext!r}")

            if self.IN_TEST:
                print(f"IN_TEST: skipping post for {label}")
                responses.append((payload["metadata"]["function_name"], outfile))
                continue

            resp = post_raster(
                post_url=output["post_url"],
                filepath=outfile,
                parameter=output.get("parameter", payload["post"].get("parameter", "")),
                layer=output.get("layer", payload["post"].get("layer", "")),
                filename=output.get("filename") or os.path.basename(outfile),
                extent=extent,
            )
            if resp.ok:
                responses.append(f"{label} file posted successfully.")
            else:
                responses.append(f"Error posting {label} file, status: {resp.status_code}.")
        return responses

    def _post_scalar_from_payload(self, payload, parameter: str, value: float, units: str):
        scalar_url = payload["post"].get("scalar_url")
        if not scalar_url:
            raise ValueError(
                "Scalar job without scalar_url in post block: "
                f"function={payload['metadata'].get('function_name')!r}",
            )
        if self.IN_TEST:
            print(f"IN_TEST: skipping scalar post {parameter}={value} {units}")
            return [(payload["metadata"]["function_name"], value, units)]
        resp = post_scalar(
            scalar_url=scalar_url,
            parameter=parameter,
            value=value,
            units=units,
            lambda_function=payload["metadata"].get("function_name", ""),
            source="usgs_10m",
        )
        return [f"Scalar posted: {parameter}={value} ({resp.status_code})"]

    def trim_and_post(self, key, colormap, payload):
        """Clip a cached watershed raster to the field and post."""
        raster = self.cache["watershed"][key]
        result = self.gdal_warp_clip(raster, self.cache["field_shp"])
        return self.handle_posting(new_file=result, color_map=colormap, payload=payload)

    # -----------------------------------------------------------------
    #                 Named Lambda Geo Functions
    # -----------------------------------------------------------------
    #
    # Each function takes a single `payload` (the per-job event dict)
    # and returns whatever `handle_posting` / `_post_scalar_from_payload`
    # returns. The name MUST match the `function_name` in the event
    # metadata, and the equivalent constant in the Django side
    # (`tier2apps/topography/schema.py`).

    def elev_public_10m(self, payload):
        self.build_common_cache(payload)
        result = self.gdal_warp_clip(self.cache["dem_raster"], self.cache["field_shp"])
        return self.handle_posting(new_file=result, color_map="elevation", payload=payload)

    def slope_public_10m(self, payload):
        self.build_common_cache(payload)
        proj_buffered_dem = self.gdal_translate_elev(self.cache["clipped_raster"])
        slope_raster = self.slope(proj_buffered_dem, xyunit="m", zunit="m")
        geo_slope_raster = self.gdal_warp_to_geo(slope_raster)
        result = self.gdal_warp_clip(geo_slope_raster, self.cache["field_shp"])
        return self.handle_posting(new_file=result, color_map="slope", payload=payload)

    def topo_blended_public_10m(self, payload):
        """Default topography artifacts — a clipped single-band DEM TIF
        (the elevation data we use for on-demand calculations) and a
        blended-hillshade PNG (multidirectional hillshade composited over
        a color-relief elevation map; LabCore's "USGS 10m topography
        layer").

        Both share the clipped DEM's extent; `extent` is derived from the
        source tif and sent on every postback so Django can place the
        georef-less PNG precisely.
        """
        self.build_common_cache(payload)
        elev = self.gdal_warp_clip(self.cache["dem_raster"], self.cache["field_shp"])
        extent = _raster_bounds_4326(elev)
        blended_png = None

        responses = []
        for output in payload["post"]["output"]:
            ext = (output.get("extension") or "").lower()
            if ext == "tif":
                outfile = elev
                label = "TIF"
            elif ext == "png":
                if blended_png is None:
                    blended_png = GeoColorize(
                        folder=self.tmpdirname.rstrip("/"),
                    ).blended_topo(elev)
                outfile = blended_png
                label = "PNG"
            else:
                raise ValueError(f"Unknown output extension: {ext!r}")

            if self.IN_TEST:
                print(f"IN_TEST: skipping post for {label}")
                responses.append((payload["metadata"]["function_name"], outfile))
                continue

            resp = post_raster(
                post_url=output["post_url"],
                filepath=outfile,
                parameter=output.get("parameter", payload["post"].get("parameter", "")),
                layer=output.get("layer", payload["post"].get("layer", "")),
                filename=output.get("filename") or os.path.basename(outfile),
                extent=extent,
            )
            responses.append(
                f"{label} file posted successfully."
                if resp.ok
                else f"Error posting {label} file, status: {resp.status_code}.",
            )
        return responses

    def watershed_drainage(self, payload):
        self.watershed_common(payload)
        return self.trim_and_post("drainage", "drainage", payload)

    def watershed_streambeds(self, payload):
        self.watershed_common(payload)
        return self.trim_and_post("stream", "default", payload)

    def watershed_spi(self, payload):
        self.watershed_common(payload)
        return self.trim_and_post("spi", "default", payload)

    def watershed_tci(self, payload):
        self.watershed_common(payload)
        return self.trim_and_post("tci", "tci", payload)

    # ----- NEW: L / LS raster jobs -------------------------------------

    def watershed_length_slope_raster(self, payload):
        """Export the L (slope length) raster from r.watershed."""
        self.watershed_common(payload)
        return self.trim_and_post("length_slope", "length_slope", payload)

    def watershed_slope_steepness_raster(self, payload):
        """Export the LS (slope steepness) raster from r.watershed."""
        self.watershed_common(payload)
        return self.trim_and_post("slope_steepness", "slope_steepness", payload)

    # ----- NEW: L / LS scalar-reducer jobs ------------------------------

    def watershed_length_slope(self, payload):
        """Scalar reducer: mean L over the field polygon."""
        self.watershed_common(payload)
        clipped = self.gdal_warp_clip(self.cache["watershed"]["length_slope"], self.cache["field_shp"])
        value, units = reduce_length_slope(clipped)
        return self._post_scalar_from_payload(
            payload, parameter="watershed_length_slope", value=value, units=units,
        )

    def watershed_slope_steepness(self, payload):
        """Scalar reducer: mean LS over the field polygon."""
        self.watershed_common(payload)
        clipped = self.gdal_warp_clip(self.cache["watershed"]["slope_steepness"], self.cache["field_shp"])
        value, units = reduce_slope_steepness(clipped)
        return self._post_scalar_from_payload(
            payload, parameter="watershed_slope_steepness", value=value, units=units,
        )

    # ----- NEW: contour vector lines -----------------------------------

    @staticmethod
    def _tag_contour_features(geojson_path: str, index_step: int = 10) -> None:
        """
        Rewrite `geojson_path` in place: rename per-feature `level` to
        `level_ft` and tag each feature with `is_index` (True when the
        contour value is a multiple of `index_step`).
        """
        with open(geojson_path) as f:
            data = json.load(f)
        for feature in data.get("features", []):
            props = feature.setdefault("properties", {})
            level = props.pop("level", props.get("level_ft"))
            if level is None:
                continue
            level_ft = float(level)
            props["level_ft"] = level_ft
            props["is_index"] = (level_ft % index_step == 0)
        with open(geojson_path, "w") as f:
            json.dump(data, f)

    def _buffer_field_shapefile(self, field_shp: str, buffer_ft: float = 50.0) -> str:
        """
        Buffer the field shapefile by `buffer_ft` feet in EPSG:5072
        (Albers, meters) and write the result back as EPSG:4326. Returns
        the buffered shapefile path.

        Buffering in degrees would be wrong — 50 ft is not a degree
        distance and varies with latitude. We mirror `gdal_translate_elev`'s
        proj4-string approach to avoid GDAL 3 axis-order pitfalls.
        """
        buffer_m = buffer_ft * 0.3048

        src_srs = osr.SpatialReference()
        if src_srs.ImportFromProj4(
            "+proj=longlat +ellps=WGS84 +datum=WGS84 +no_defs",
        ) != 0:
            raise RuntimeError("could not import EPSG:4326")
        planar_srs = osr.SpatialReference()
        if planar_srs.ImportFromProj4(
            "+proj=aea +lat_1=29.5 +lat_2=45.5 +lat_0=23 +lon_0=-96 +x_0=0 +y_0=0 "
            "+ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs",
        ) != 0:
            raise RuntimeError("could not import EPSG:5072")

        to_planar = osr.CoordinateTransformation(src_srs, planar_srs)
        to_geo = osr.CoordinateTransformation(planar_srs, src_srs)

        src_ds = ogr.Open(field_shp)
        if src_ds is None:
            raise RuntimeError(f"Could not open field shapefile: {field_shp}")
        src_layer = src_ds.GetLayer()

        driver = ogr.GetDriverByName("ESRI Shapefile")
        out_path = field_shp.replace(".shp", f"_buf{int(buffer_ft)}ft.shp")
        if os.path.exists(out_path):
            driver.DeleteDataSource(out_path)
        out_ds = driver.CreateDataSource(out_path)
        out_layer = out_ds.CreateLayer(
            "buffered", srs=src_srs, geom_type=ogr.wkbPolygon,
        )
        out_defn = out_layer.GetLayerDefn()

        for feature in src_layer:
            geom = feature.GetGeometryRef().Clone()
            geom.Transform(to_planar)
            buffered = geom.Buffer(buffer_m)
            buffered.Transform(to_geo)
            out_feature = ogr.Feature(out_defn)
            out_feature.SetGeometry(buffered)
            out_layer.CreateFeature(out_feature)
            out_feature = None

        out_layer = None
        out_ds = None
        src_ds = None
        return out_path

    def _post_vector_outputs(self, file_path: str, payload) -> list:
        """Post a vector file to every `output` slot in `payload['post']`."""
        responses = []
        for output in payload["post"]["output"]:
            if self.IN_TEST:
                print(f"IN_TEST: skipping vector post for {output.get('parameter')}")
                responses.append((payload["metadata"]["function_name"], file_path))
                continue
            resp = post_raster(
                post_url=output["post_url"],
                filepath=file_path,
                parameter=output.get("parameter", payload["post"].get("parameter", "")),
                layer=output.get("layer", payload["post"].get("layer", "")),
                filename=output.get("filename") or os.path.basename(file_path),
            )
            responses.append(
                "GeoJSON posted successfully."
                if resp.ok
                else f"Error posting GeoJSON, status: {resp.status_code}.",
            )
        return responses

    def contour_lines(self, payload):
        """
        Generate 2 ft contour lines (with 10 ft index contours) from the
        clipped DEM, clipped to a 50 ft buffer around the field boundary.

        Output: single GeoJSON FeatureCollection. Each feature has
        `level_ft` (the contour value in feet) and `is_index` (True for
        multiples of 10 ft). Per-feature geometries fall within
        `field_shp.buffer(50 ft)` — they extend past the fenceline so
        technicians can see contour direction at the edge.
        """
        self.build_common_cache(payload)

        outdir = os.path.join(self.tmpdirname.rstrip("/"), "contours")
        os.makedirs(outdir, exist_ok=True)

        raw_geojson = get_contour_lines(
            self.cache["clipped_raster"], outdir, step_ft=2,
        )
        self._tag_contour_features(raw_geojson, index_step=10)

        buffered_shp = self._buffer_field_shapefile(
            self.cache["field_shp"], buffer_ft=50,
        )

        clipped_geojson = os.path.join(
            outdir, f"contours_clipped_{os.getpid()}.geojson",
        )
        if os.path.exists(clipped_geojson):
            os.remove(clipped_geojson)
        subprocess.check_call(
            f'ogr2ogr -clipsrc "{buffered_shp}" -f GeoJSON '
            f'"{clipped_geojson}" "{raw_geojson}"',
            shell=True,
        )

        return self._post_vector_outputs(clipped_geojson, payload)

    # ----- NEW: MFD flow lines (RUSLE2 slope-shooting basemap) ---------

    _M_PER_FT = 0.3048
    _FT_PER_M = 1 / 0.3048
    _ACRES_PER_M2 = 1 / 4046.8564224

    @staticmethod
    def _dp_simplify(points, epsilon):
        """
        Douglas-Peucker on a sequence of (x, y) tuples. Returns the
        simplified subsequence (always preserves first + last points).
        """
        if len(points) <= 2:
            return list(points)

        p0 = points[0]
        pN = points[-1]
        dx_line = pN[0] - p0[0]
        dy_line = pN[1] - p0[1]
        line_len_sq = dx_line * dx_line + dy_line * dy_line

        max_dist = 0.0
        max_idx = 0
        if line_len_sq == 0:
            # Degenerate line — fall back to keeping only endpoints
            return [points[0], points[-1]]

        for i in range(1, len(points) - 1):
            px, py = points[i]
            # 2D cross-product magnitude / line length = perpendicular distance
            cross = abs(dx_line * (p0[1] - py) - (p0[0] - px) * dy_line)
            dist = cross / math.sqrt(line_len_sq)
            if dist > max_dist:
                max_dist = dist
                max_idx = i

        if max_dist > epsilon:
            left = LambdaGISProcessor._dp_simplify(
                points[:max_idx + 1], epsilon,
            )
            right = LambdaGISProcessor._dp_simplify(
                points[max_idx:], epsilon,
            )
            return left[:-1] + right
        return [points[0], points[-1]]

    @staticmethod
    def _simplify_profile(
        profile_ft,
        epsilon_ft: float = 0.5,
        max_segments: int = 6,
        min_seg_length_ft: float = 25,
    ) -> list:
        """
        Simplify a `(cum_length_ft, elev_ft)` profile into ≤ `max_segments`
        segments. Returns `[{length_ft, slope_pct}, ...]`.

        Steps:
          1. Douglas-Peucker with `epsilon_ft` on the elevation axis.
          2. If still more than `max_segments`, iteratively raise epsilon.
          3. Merge any segment shorter than `min_seg_length_ft` into a
             neighbor.
        """
        if not profile_ft or len(profile_ft) < 2:
            return []

        points = list(profile_ft)
        eps = epsilon_ft
        simplified = LambdaGISProcessor._dp_simplify(points, eps)
        for _ in range(20):  # safety cap
            if len(simplified) - 1 <= max_segments:
                break
            eps *= 1.5
            simplified = LambdaGISProcessor._dp_simplify(points, eps)

        # Merge short segments
        pts = list(simplified)
        changed = True
        while changed and len(pts) > 2:
            changed = False
            for i in range(len(pts) - 1):
                seg_len = abs(pts[i + 1][0] - pts[i][0])
                if seg_len < min_seg_length_ft:
                    if i == 0:
                        pts.pop(1)
                    elif i == len(pts) - 2:
                        pts.pop(-2)
                    else:
                        pts.pop(i + 1)
                    changed = True
                    break

        segments = []
        for i in range(len(pts) - 1):
            seg_len = abs(pts[i + 1][0] - pts[i][0])
            if seg_len <= 0:
                continue
            slope_pct = ((pts[i + 1][1] - pts[i][1]) / seg_len) * 100
            segments.append({
                "length_ft": round(seg_len, 1),
                "slope_pct": round(slope_pct, 2),
            })
        return segments

    @staticmethod
    def _classify_profile_shape(segments) -> str:
        """
        Label a simplified profile as uniform / concave / convex / complex.

        - uniform   — single segment, OR slope std / |mean| < 15 %
        - convex    — slope monotonically INCREASES downhill (terrain steepens)
        - concave   — slope monotonically DECREASES downhill (terrain flattens)
        - complex   — multiple sign changes in the slope-change sequence
        """
        if not segments:
            return "uniform"
        slopes = [s["slope_pct"] for s in segments]
        if len(slopes) == 1:
            return "uniform"

        arr = np.asarray(slopes, dtype=float)
        mean = float(arr.mean())
        if mean == 0:
            relative_std = 0.0 if arr.std() == 0 else float("inf")
        else:
            relative_std = abs(float(arr.std()) / mean)
        if relative_std < 0.15:
            return "uniform"

        diffs = np.diff(arr)
        sign_changes = 0
        last_sign = 0
        for d in diffs:
            if abs(d) < 1e-9:
                continue
            sign = 1 if d > 0 else -1
            if last_sign != 0 and sign != last_sign:
                sign_changes += 1
            last_sign = sign

        if sign_changes >= 2:
            return "complex"
        if (diffs >= 0).all():
            return "convex"
        if (diffs <= 0).all():
            return "concave"
        return "complex"

    @staticmethod
    def _read_first_geom(shp_path: str):
        """Return the first feature's geometry from a shapefile, cloned."""
        ds = ogr.Open(shp_path)
        if ds is None:
            return None
        layer = ds.GetLayer()
        feat = layer.GetNextFeature()
        if feat is None:
            return None
        return feat.GetGeometryRef().Clone()

    def _clip_wkt_to_cutline(self, wkt: str, cutline_geom):
        """
        Intersect a WKT geometry with a polygon cutline (an OGR geom).
        Returns clipped WKT or None if empty.
        """
        if not wkt or wkt == "POLYGON EMPTY" or cutline_geom is None:
            return None
        g = ogr.CreateGeometryFromWkt(wkt)
        if g is None:
            return None
        clipped = g.Intersection(cutline_geom)
        if clipped is None or clipped.IsEmpty():
            return None
        return clipped.ExportToWkt()

    @staticmethod
    def _planar_area_acres(wkt: str) -> float:
        """Compute the area of a Polygon WKT (EPSG:4326) in acres via EPSG:5072."""
        if not wkt or wkt == "POLYGON EMPTY":
            return 0.0
        g = ogr.CreateGeometryFromWkt(wkt)
        if g is None or g.IsEmpty():
            return 0.0
        src_srs = osr.SpatialReference()
        src_srs.ImportFromProj4("+proj=longlat +ellps=WGS84 +datum=WGS84 +no_defs")
        planar_srs = osr.SpatialReference()
        planar_srs.ImportFromProj4(
            "+proj=aea +lat_1=29.5 +lat_2=45.5 +lat_0=23 +lon_0=-96 +x_0=0 +y_0=0 "
            "+ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs",
        )
        tx = osr.CoordinateTransformation(src_srs, planar_srs)
        clone = g.Clone()
        clone.Transform(tx)
        return float(clone.GetArea()) * LambdaGISProcessor._ACRES_PER_M2

    def mfd_flowlines(self, payload):
        """
        Representative RUSLE2 hillslope profiles plus the field's draw
        network, as one GeoJSON FeatureCollection. Features are tagged by
        `kind`:

        - `hillslope`: ridge→channel transect — D8 path geometry, D8
          catchment polygon (WKT in properties), simplified slope profile
          (≤ 6 segments), shape classification, accumulation rank. Sheet &
          rill erosion (RUSLE2 LS).
        - `draw`: a concentrated-flow channel traced head→outlet — geometry,
          length, accumulation rank. Ephemeral-gully erosion.

        Empty FeatureCollection on flat fields — see ENGINE.md / spec.
        """
        self.build_common_cache(payload)

        outdir = os.path.join(self.tmpdirname.rstrip("/"), "flowlines")
        os.makedirs(outdir, exist_ok=True)

        options = (payload.get("metadata") or {}).get("input_data") or {}
        raw_lines = get_mfd_flowlines_raw(
            self.cache["watershed_raster"], outdir, options=options,
            field_shp=self.cache["field_shp"],
        )

        # Build cutline geometries up front. Lines clip to field+50 ft,
        # zones clip to field exactly (area-weighting math depends on it).
        buffered_shp = self._buffer_field_shapefile(
            self.cache["field_shp"], buffer_ft=50,
        )
        line_cutline = self._read_first_geom(buffered_shp)
        zone_cutline = self._read_first_geom(self.cache["field_shp"])

        max_segments = int(options.get("profile_segment_max_count", 6))
        min_seg_length_ft = float(options.get("profile_segment_min_length_ft", 25))

        features = []
        for raw in raw_lines:
            # Draw records (concentrated-flow channels) carry only a line —
            # no slope profile or hillslope zone. Clip to field + 50 ft and
            # emit; MultiLineString is allowed (a draw may cross the fence).
            if raw.get("kind") == "draw":
                clipped = self._clip_wkt_to_cutline(
                    raw["line_wkt"], line_cutline,
                )
                if clipped is None:
                    continue
                draw_geom = ogr.CreateGeometryFromWkt(clipped)
                if draw_geom is None or draw_geom.IsEmpty():
                    continue
                draw_profile = raw.get("profile_vertices_m") or []
                draw_len_ft = (
                    round(draw_profile[-1][0] * self._FT_PER_M, 1)
                    if draw_profile else 0.0
                )
                features.append({
                    "type": "Feature",
                    "geometry": json.loads(draw_geom.ExportToJson()),
                    "properties": {
                        "kind": "draw",
                        "rank": raw["rank"],
                        "flow_accumulation_cells": raw["flow_accumulation_cells"],
                        "total_length_ft": draw_len_ft,
                        "source": "auto_mfd",
                    },
                })
                continue

            # Convert profile to feet
            profile_ft = [
                (cum_m * self._FT_PER_M, elev_m * self._FT_PER_M)
                for (cum_m, elev_m) in raw.get("profile_vertices_m", [])
            ]
            segments = self._simplify_profile(
                profile_ft,
                epsilon_ft=0.5,
                max_segments=max_segments,
                min_seg_length_ft=min_seg_length_ft,
            )
            shape = self._classify_profile_shape(segments)
            max_slope_pct = (
                max(abs(s["slope_pct"]) for s in segments) if segments else 0.0
            )
            total_length_ft = sum(s["length_ft"] for s in segments)

            # Clip line to field+50 ft buffer
            clipped_line_wkt = self._clip_wkt_to_cutline(
                raw["line_wkt"], line_cutline,
            )
            if clipped_line_wkt is None:
                # Line fully outside the visibility buffer — shouldn't happen
                # for paths seeded inside the field, but drop defensively.
                continue

            # Clip zone to field; recompute area in planar coords
            clipped_zone_wkt = self._clip_wkt_to_cutline(
                raw["zone_wkt"], zone_cutline,
            )
            if clipped_zone_wkt is None:
                clipped_zone_wkt = "POLYGON EMPTY"
                zone_area_ac = 0.0
            else:
                zone_area_ac = self._planar_area_acres(clipped_zone_wkt)

            # Feature geometry is the clipped line (so it renders directly
            # in MapLibre etc.). The zone polygon rides as WKT property —
            # consumer parses if it wants the catchment.
            line_geom = ogr.CreateGeometryFromWkt(clipped_line_wkt)
            if line_geom is None or line_geom.GetGeometryType() not in (
                ogr.wkbLineString,
                ogr.wkbLineString25D,
            ):
                # Intersection produced a MultiLineString or empty — drop.
                continue
            line_geojson = json.loads(line_geom.ExportToJson())

            features.append({
                "type": "Feature",
                "geometry": line_geojson,
                "properties": {
                    "kind": "hillslope",
                    "rank": raw["rank"],
                    "flow_accumulation_cells": raw["flow_accumulation_cells"],
                    "total_length_ft": round(total_length_ft, 1),
                    "max_slope_pct": round(max_slope_pct, 2),
                    "profile_shape": shape,
                    "profile_segments": segments,
                    "zone_geometry_wkt": clipped_zone_wkt,
                    "zone_area_ac": round(zone_area_ac, 3),
                    "source": "auto_mfd",
                },
            })

        collection = {"type": "FeatureCollection", "features": features}
        out_path = os.path.join(
            outdir, f"flowlines_{os.getpid()}.geojson",
        )
        with open(out_path, "w") as fh:
            json.dump(collection, fh)

        return self._post_vector_outputs(out_path, payload)

    # ----- legacy: rasterize_and_colorize -------------------------------

    def rasterize_and_colorize(self, payload):
        """
        Download zipped shapefile from S3, run gdal_grid interpolation,
        rasterize and colorize. Carried over from USGS2021.
        """
        directory = None
        try:
            bucket = payload["metadata"]["input_data"]["shapefile_location"]["bucket"]
            key = payload["metadata"]["input_data"]["shapefile_location"]["key"]
            layer = payload["metadata"]["input_data"]["layer"]
            shapefile = payload["metadata"]["input_data"]["shapefile"]
            parameter = payload["metadata"]["input_data"]["parameter"]

            field_shp = self.get_shapefile_from_geojson_ogr(payload["metadata"]["field_boundary"])
            fname = download_and_unzip(bucket, key)
            src = get_path(os.path.dirname(fname), shapefile)
            directory = os.path.dirname(fname)
            tif = f"{directory}/{parameter}.tif"
            r1 = self.xPixel * 2
            alg = (
                f"invdistnn:power=2.0:smoothing=0.0:radius={r1}:"
                f"max_points=12:min_points=0:nodata=-9999.0"
            )
            lngmin, latmin, lngmax, latmax = self.get_shapefile_extent(src, close=True)
            gridy = int((latmax - latmin) / self.xPixel)
            gridx = int((lngmax - lngmin) / self.yPixel)
            cmd = (
                f'gdal_grid -q -l "{layer}" -zfield "{parameter}" -a {alg} '
                f'-ot Float32 -of GTiff -a_srs epsg:4326 '
                f'-txe {lngmin} {lngmax} -tye {latmax} {latmin} '
                f'-outsize {gridx} {gridy} "{src}" "{tif}"'
            )
            assert subprocess.call(cmd, shell=True) == 0, "Failed grid generation"

            tif_clip = self.gdal_warp_clip(tif, field_shp)
            with open(directory + "/color.txt", "w") as f:
                f.write(payload["metadata"]["input_data"]["colors"])

            png = f"{directory}/{parameter}.png"
            colorize = (
                f'gdaldem color-relief -q -of PNG "{tif_clip}" '
                f'"{directory}/color.txt" "{png}" -nearest_color_entry -alpha'
            )
            assert subprocess.call(colorize, shell=True) == 0, "Failed colorizing"

            # png and tif_clip share the clipped tif's bounds.
            extent = _raster_bounds_4326(tif_clip)
            for output in payload["post"]["output"]:
                outfile = png if output["extension"] == "png" else tif_clip
                if not self.IN_TEST:
                    post_raster(
                        post_url=output["post_url"],
                        filepath=outfile,
                        parameter=output.get("parameter", parameter),
                        layer=output.get("layer", ""),
                        filename=output.get("filename") or os.path.basename(outfile),
                        extent=extent,
                    )

            return tif_clip, png

        except Exception as exc:
            print("rasterize_and_colorize", repr(exc))
            raise
        finally:
            if directory and not self.IN_TEST:
                try:
                    shutil.rmtree(directory)
                except Exception as exc:
                    print("Unable to delete tempdir", repr(exc))


# ---------------------------------------------------------------------------
# Entry points used by the SQS handler
# ---------------------------------------------------------------------------

def process_payload(payload: list) -> list:
    """
    Run every job in `payload` (the parsed SQS message body — a list of
    per-job event dicts). Returns a list of human-readable result strings,
    one per job.
    """
    gis = LambdaGISProcessor()
    _cleanup("/tmp/*")

    results = []
    for job in payload:
        fn_name = job["metadata"]["function_name"]
        fn = getattr(gis, fn_name, None)
        if not callable(fn):
            raise NotImplementedError(
                f"LambdaGISProcessor has no function named {fn_name!r}",
            )
        try:
            result = fn(job)
        except Exception as exc:
            print(f"Job function error: {fn_name} → {exc}")
            result = "ERROR"
        site_prefix = job["metadata"].get("site_prefix", "agkit")
        field_id = job["metadata"].get("field_id")
        results.append(
            f"{site_prefix} Field ID <{field_id}> Lambda Job Function "
            f"named: {fn_name} resulted in {result}.",
        )

    if gis.cleanup:
        _cleanup("/tmp/*.*")

    print(results)
    return results


def main_from_file(job_file: str) -> list:
    """Local-run convenience: load payload from a JSON file on disk."""
    with open(job_file, "r") as f:
        payload = json.load(f)
    return process_payload(payload)
