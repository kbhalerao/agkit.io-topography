# Flow lines (MFD) — feature spec

Extract 1–N representative flow paths over the field area using Multiple
Flow Direction routing on the DEM, plus a per-line contributing-catchment
polygon, plus a per-line slope profile sampled along the path. Primary
consumer: the erosion app's slope-shooting UI, where each suggested line
seeds a draggable polyline the technician can accept, edit, or override.

Pairs with `contour-feature.md` — contours give the technician the visual
basemap; flow lines give them a starting point. Same Lambda pipeline; both
postback through the existing topography postback endpoint.

Not yet implemented.

## Why MFD (not D8)

D8 routes a cell's flow to a single downslope neighbor — produces sharp,
discrete paths that work well on dissected terrain but tend to grid-align
and miss subtle convergence on the gentle Corn Belt slopes that dominate
the customer base.

MFD (Quinn et al. 1991 / Freeman 1991, implemented by GRASS `r.watershed -m`)
distributes flow proportionally across all downslope neighbors weighted by
slope steepness. On low-relief terrain it produces lines that follow real
topographic concavities rather than DEM-grid artefacts. That is the
defensible behavior for an NRCS reviewer eyeballing the output against the
contour map.

For **catchment polygons** we use **D8**, which is the physically correct
choice for this use case — not a fallback. RUSLE2 area-weighting (each
profile's erosion intensity × its catchment area, summed over the field)
requires zones that tile the field exclusively: every cell must belong to
one and only one catchment. D8 produces exactly that. MFD catchments are
fractional — a cell distributes its runoff across multiple downslope
neighbors weighted by slope — so they have no clean polygon representation
and can't be area-weighted without double-counting.

NRCS technician practice is D8-shaped too. When a reviewer mentally
identifies management areas on a field, they're tracing ridge boundaries
between mutually exclusive watersheds. The D8 output matches that thirty-
year-old mental model; MFD outputs wouldn't.

So: **MFD for the path** (where dominant flow goes — accurate on gentle
terrain) and **D8 for the catchment** (where the area-weighting math must
close). Both physically defensible for their respective uses. No caveat
needed in the planner UI.

## Data contract

The Lambda emits **one GeoJSON FeatureCollection per field**. Each feature
is a LineString (the flow path) with the catchment polygon and profile
embedded in properties:

```json
{
  "type": "Feature",
  "geometry": {"type": "LineString", "coordinates": [...]},
  "properties": {
    "rank": 1,
    "flow_accumulation_cells": 247,
    "total_length_ft": 612.4,
    "max_slope_pct": 4.8,
    "profile_shape": "concave",
    "profile_segments": [
      {"length_ft": 80.0, "slope_pct": 2.1},
      {"length_ft": 220.5, "slope_pct": 3.4},
      {"length_ft": 311.9, "slope_pct": 5.8}
    ],
    "zone_geometry_wkt": "POLYGON((...))",
    "zone_area_ac": 4.62,
    "source": "auto_mfd"
  }
}
```

Property notes:

- `rank` — 1 = strongest path (largest contributing area), monotonically
  increasing.
- `flow_accumulation_cells` — count of upslope cells. Used by the planner
  UI to size the line weight when rendered.
- `profile_segments` — already Douglas-Peucker simplified to 3–6 segments
  in the Lambda. The erosion daughter passes them straight to romex as the
  SLOPE input without further processing.
- `profile_shape` — classification: `uniform` (slope std < 15%), `concave`
  (slope decreases downhill), `convex` (slope increases downhill),
  `complex` (multi-peak). Used by the UI to label the line and by RUSLE2
  to set deposition routing.
- `zone_geometry_wkt` — WKT string (not nested GeoJSON) because the
  consumer stores it in a parallel-array column on `FlowLineLayer.document`.
  Lambda emits WKT to keep the parallel-column pattern clean.
- `source` — always `"auto_mfd"` for this handler. Planner-edited lines
  retag to `"planner_edited"` on save in the daughter.

## Inputs (payload)

Same shape as other topography work types — extends the SQS payload that
already carries field geometry. New options block:

```json
{
  "field_id": 1234,
  "field_geometry": {"type": "Polygon", "coordinates": [...]},
  "options": {
    "dem_source": "public_10m",
    "max_lines": 5,
    "min_flow_accumulation_cells": 50,
    "min_line_length_ft": 100,
    "profile_segment_max_count": 6,
    "profile_segment_min_length_ft": 25
  }
}
```

Defaults if the options block is missing: `max_lines=5`,
`min_flow_accumulation_cells=50` (≈ 5 000 m² catchment at 10 m DEM),
`min_line_length_ft=100`, `profile_segment_max_count=6`,
`profile_segment_min_length_ft=25`.

`dem_source` initially supports only `public_10m`. LiDAR / 1 m support
arrives the day a LiDAR DEM ingestion path exists; same handler, different
input raster.

## Buffer & clip

- Reuse `clipped_raster` from `build_common_cache()` — 220 m bbox-buffered
  DEM. The buffer matters more here than for contours: an MFD path that
  exits the field needs the downstream cells visible so the flow direction
  is computed correctly at the boundary, otherwise paths terminate
  abruptly at the fence in a way that looks wrong on the map.
- **Lines** are clipped to `field_shp.buffer(50 ft)` (same as contours) so
  the line stays visible across the fence but doesn't shoot off into the
  neighbor's field.
- **Zone polygons** are clipped to `field_shp` (no buffer) — area-weighting
  must sum to the field area, not the field area plus a buffer.
- Buffer + clip both happen in a planar CRS (3857 or local UTM), not in
  degrees.

## Lambda side — `agkit-topography`

New method `LambdaGISProcessor.mfd_flowlines(self, payload)` in
`app/geoworker.py`:

**Shared caching:** `r.fill.dir`'s outputs (`filled_dem` + `d8_dir`) should
live in `build_common_cache` so future GRASS-routed jobs don't recompute
them. The existing contour code runs on the unfilled DEM and is fine —
sink-filling matters for routing, not for raw elevation contours; lift it
when a third consumer arrives.

1. `self.build_common_cache(payload)` — reuses 220 m-buffered DEM.
2. `r.fill.dir input=clipped_raster output=filled_dem direction=d8_dir
   areas=fill_areas` — fill sinks so MFD doesn't dead-end in pits. The
   `direction` output is a **D8 flow-direction raster as a byproduct** —
   reused for catchment delineation in step 8, avoiding a second
   `r.watershed` pass.
3. `r.watershed -m elevation=filled_dem accumulation=flow_acc threshold=N`
   with `N = min_flow_accumulation_cells`. `-m` enables MFD mode. We only
   need the accumulation raster from this pass; the drainage direction we
   use (D8) came from step 2.
4. Pick line seeds from the top of `flow_acc`. Read `flow_acc` into numpy;
   take the top `max_lines × 4` cells by value as seed candidates; apply
   non-maximum suppression with a `min_line_length_ft / 2` radius (in DEM
   cells) so seeds aren't clustered along the same dominant channel; keep
   the top `max_lines` survivors.

   *(This replaces an earlier draft that used `r.thin` + `r.to.vect` +
   Python graph traversal to reconstruct paths from a thinned channel
   raster. Seeding from accumulation peaks and tracing each downhill is
   simpler, deterministic, and avoids edge-case behavior when channels
   branch or rejoin.)*
5. Trace each seed downhill: `r.drain input=filled_dem direction=d8_dir
   start_coordinates=<seed_x,seed_y> output=line_<rank>` produces one
   LineString per seed following D8 drainage to the basin outlet.
   (`r.drain` accepts our `d8_dir` from step 2, so the path geometry uses
   the same routing the catchment will in step 8 — line and zone agree on
   "downhill" at every cell.)
6. Convert each `line_<rank>` raster to vector with `r.to.vect type=line`.
   Drop lines where the resulting vector has fewer than 2 vertices or
   `total_length_ft < min_line_length_ft`. Reject lines that come out as
   MultiLineString — they crossed a no-data hole and the routing through
   it is unreliable; log and skip.
7. For each kept line:
   - Sample DEM elevation at every line vertex (use `v.what.rast`).
   - Build `(cumulative_length_ft, elevation_ft)` from the vertex sequence,
     accounting for diagonal vs cardinal cell steps (√2 vs 1 at the DEM
     resolution).
   - Douglas-Peucker the elevation curve with **epsilon = 0.5 ft** on the
     elevation axis (cumulative length is x, elevation is y). Then cap at
     `profile_segment_max_count` by raising epsilon iteratively if more
     segments survive, and merge any segment shorter than
     `profile_segment_min_length_ft` into its neighbor.
   - Compute `max_slope_pct = max(|dz/dx|)`, classify `profile_shape`
     per the thresholds in the "Data contract" section.
8. For each kept line, compute its catchment with a single
   `r.water.outlet input=d8_dir output=catchment_<rank>
   coordinates=<outlet_x,outlet_y>` at the line's downstream endpoint
   (last vertex). `r.water.outlet` traces upstream from one cell along the
   D8 direction raster — no per-pixel iteration over the line.
   Vectorize the catchment raster (`r.to.vect type=area`), clip to
   `field_shp` (no buffer — area-weighting must sum to the field area),
   compute `zone_area_ac` from the clipped polygon area.
9. `v.out.ogr format=GeoJSON` the lines; post-process to merge in catchment
   WKT + profile segments per feature.
10. Post back via `Poster.post_raster(...)` — `.geojson` file extension is
    the routing signal on the receiving side. Same auth machinery as contours.

### Edge-case behaviors (locked defaults)

- **DP epsilon**: 0.5 ft elevation on the profile curve (step 7). Iteratively
  raise to fit `profile_segment_max_count`; merge segments shorter than
  `profile_segment_min_length_ft`.
- **No qualifying lines**: if no cells exceed `min_flow_accumulation_cells`
  (flat field) or all candidates fail the length filter, the Lambda emits
  an **empty FeatureCollection** and posts it normally. Empty is
  semantically different from "job failed" — the consumer learns the job
  completed and produced zero suggestions.
- **MultiLineString rejection**: drop and log (step 6). Routing through a
  no-data hole is unreliable.
- **`profile_shape` classifier needs calibration on real fields.** Land the
  spec'd defaults (slope std < 15% → `uniform`; sign of dslope/dlength →
  `concave` vs `convex`; multiple sign changes → `complex`), then revisit
  once a Corn Belt sample has been scored against NRCS reviewer judgement.

Add to `AWS_LAMBDA_EVENT_CONFIGURATION["public_elev_10m"]["jobs"]`:

```python
{"lambda_function": "mfd_flowlines",
 "post_back": {"file_extensions": ("geojson",),
               "parameter": "flowlines_mfd_public_10m",
               "layer": "USGS 10m MFD Flow Lines"}},
```

## Django side — `agkit.io-backend/tier2apps/topography/`

Reuses everything `contour-feature.md` adds — no new proxy model, no new
endpoint, no new auth path. The discriminator is the `parameter` value.

### `schema.py`

Add to the canonical-parameter block:

```python
PARAM_FLOWLINES_MFD_10M = "flowlines_mfd_public_10m"
```

Extend `ALL_VECTOR_PARAMETERS`:

```python
ALL_VECTOR_PARAMETERS = (PARAM_CONTOURS_10M, PARAM_FLOWLINES_MFD_10M)
```

### `services.py`

**No changes.** `upsert_topography_vector` already exists from the contour
feature, and `GISVectorLayer.from_geojson(overwrite=True)` handles flow
lines without modification:

- Every property on every feature is automatically lifted into `document`
  as a parallel columnar array. For flow lines that means
  `document = {"rank": [1, 2, ...], "zone_geometry_wkt": [...],
  "zone_area_ac": [...], "profile_segments": [...], "profile_shape": [...],
  ...}`. The columnar shape is built into `GISVectorLayer`; no row-form
  `features` array exists.
- The `geometry` `GeometryCollection` and each column in `document` are
  same-ordered by construction (`from_geojson` iterates the input features
  once and appends to both). The erosion daughter joins by index into
  `geometry[i]` and `document['rank'][i]` etc.
- `metadata['columns']` is set by `from_geojson` to the list of
  property names, so downstream `to_geojson()` round-trips cleanly.

### `api_views.py`

No change. `RasterPostbackView` already dispatches `.geojson` files to
`upsert_topography_vector` after the contour feature lands.

### Tests

- `test_services.py`: feed a synthetic GeoJSON FeatureCollection with three
  LineStrings and the full property block; assert a single
  `TopographyVectorLayer` row is created with `metadata['parameter'] ==
  PARAM_FLOWLINES_MFD_10M`, `len(geometry) == 3`, `document['rank'] ==
  [1, 2, 3]`, `document['source'] == ['auto_mfd', 'auto_mfd', 'auto_mfd']`,
  and `metadata['columns']` contains every expected property name.
- `test_api_views.py`: post the same `.geojson` to the existing
  raster-postback URL, assert dispatch to `upsert_topography_vector` (not
  the raster path). Already covered by the contour tests; just add a
  flow-lines fixture.
- Lambda side: `tests/test_geoworker.py` — assert `mfd_flowlines` on a
  fixture DEM produces ≥ 1 line, every line has a non-empty
  `profile_segments` array, every zone polygon WKT parses, and every line
  is contained in `field_shp.buffer(50 ft)`.

## Erosion daughter side — `agkit.io-erosion` (note for future work)

A service in `apps/erosion` polls / receives webhooks for new
`TopographyVectorLayer` rows where `parameter == flowlines_mfd_public_10m`
and copies them into `apps.erosion.FlowLineLayer` (a proxy of the same
`GISVectorLayer` table). The copy step:

- Maps each GeoJSON feature into a row in the daughter's
  `FlowLineLayer.document` columns: `line_name = f"Auto MFD #{rank}"`,
  `source = 'auto_mfd'`, `profile_segments`, `profile_shape`,
  `zone_geometry_wkt`, `zone_area_ac`, etc.
- Sets `metadata['is_active'] = True` on the new layer; flips the previous
  active layer for the same field to `False`.
- Preserves any `source == 'planner_drawn'` or `'planner_edited'` lines
  the technician had added — merge, don't replace.

The mother doesn't know about `apps.erosion.FlowLineLayer`. The
`TopographyVectorLayer` row is the source-of-truth in the mother; the
daughter's copy is its working artifact.

## What we deliberately didn't do

- **No D8-only flow lines.** D8 grid artefacts look bad on the Corn Belt
  terrain that drives the buyer's first impression. MFD is the default;
  D8-only can be added later as an option if a customer asks.
- **No "snap a technician-drawn line to the nearest flow path."** That's a
  separate feature (and lives more naturally client-side over a vector
  tile of the flow accumulation raster). This processor is the
  *auto-suggest* path only.
- **No per-line ranking by anything other than upslope accumulation.**
  Tried slope-weighted ranking on an earlier prototype; produced shorter
  steep paths that planners didn't recognize as the dominant flow.
  Accumulation-weighted ranking matches the visual on a hillshade.
- **No catchment polygons for paths that exit the field.** If the path's
  outlet falls outside `field_shp`, the catchment is clipped to
  `field_shp` (above) and the resulting polygon may not be a closed
  watershed. The `zone_area_ac` is still the correct area-weighting basis
  for RUSLE2 (which only cares about the field).
- **No LiDAR DEM support in v1.** Same handler, different `dem_source`;
  ships when a LiDAR ingestion path exists.
- **No multi-field batch.** One field per Lambda invocation. The existing
  SQS fan-out handles parallelism at the queue level.
- **No deprecation of the L/LS scalar reducers** or the contour pipeline.
  Both stay; the technician-drawn profile (informed by these MFD
  suggestions) is the authoritative override path, not the only path.
