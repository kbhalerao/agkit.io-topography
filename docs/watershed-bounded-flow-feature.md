# Watershed-bounded flow calculations — feature spec

Expand the *computation domain* of every flow-routed job (L, LS, SPI, TCI,
MFD hillslopes, draws) from a fixed ~220 m buffer to a **user-selected
analysis area** — typically the field's HUC-12 watershed, but the user may
instead pick a USGS grid cell or hand-draw any polygon. Flow accumulation,
slope length, and the hillslope/draw channelization thresholds are then
computed against real upslope area instead of an arbitrary clip.

**Status (2026-05-19): Lambda side built; Django + UI sides pending.**
Decisions locked with the user on 2026-05-19 (see "Locked decisions"). The
Lambda DEM-domain change is implemented and tested — see "Lambda side"
below for what shipped and what was deferred. The Django side and the
`agkit.io-erosion` selection UI are not built. Supersedes the "Buffer &
clip" section of `flowlines-feature.md` for all flow-routed jobs.
`contour_lines`, `elev_public_10m`, `slope_public_10m`, and
`topo_blended_public_10m` are local-elevation layers and are **not**
affected.

## Locked decisions

| Decision        | Choice                                                          |
|-----------------|-----------------------------------------------------------------|
| Domain          | User-selected analysis polygon — the literal DEM compute extent |
| Selection modes | HUC-12 (auto-suggested), a USGS grid cell, or a hand-drawn area |
| HUC-12 source   | ArcGIS WBD FeatureServer, queried by Django for the suggestion  |
| Boundary transit| Inline in the SQS payload; hand-drawn selections persisted per field |
| Under-drawn area| Literal — the Lambda computes on it as-is, no auto-extend       |
| Scope           | Watershed-correct flow geometry only — no erosion grading       |

## The problem: a fixed buffer truncates the contributing area

`build_common_cache` → `gdal_bufferred_clip` (`geoworker.py`) clips the DEM
to field bbox + 0.002° (~220 m). Every flow function runs on that raster.
The buffer is arbitrary, and the consequences compound:

- **Accumulation is understated.** A cell's flow accumulation counts only
  upslope cells *inside the DEM*. Near the field's upslope edge the true
  contributing area is cut off — accumulation is biased low.
- **L is understated.** `r.watershed length_slope` measures slope length
  from the highest in-DEM cell, not the true drainage divide. RUSLE2 λ for
  a field that receives run-on comes out short.
- **The hillslope tracer can seed off a fake divide.** `get_mfd_flowlines_raw`
  walks divide→channel; on the clipped DEM a "divide" cell can be the DEM
  edge rather than a real ridge.

The bias is worst exactly where it matters most: fields low in their local
watershed, which receive the most run-on and have the longest effective
slope lengths.

## Why this also attacks the threshold problem

This is the motivating reason for the feature. `get_mfd_flowlines_raw` has
three thresholds that decide what a hillslope and a draw *are*:

| Option                        | Default | Role                                       |
|-------------------------------|---------|--------------------------------------------|
| `min_flow_accumulation_cells` | 50      | half-basin size / `r.watershed` stream def |
| `channel_threshold_cells`     | 50      | accumulation at which a hillslope ends     |
| `draw_threshold_cells`        | 300     | accumulation at which a channel is a draw  |

(`get_watershed_maps` independently hardcodes `threshold=30` for the
raster watershed outputs — another magic number this feature folds in.)

The sensitivity sweep (`tests/_gary_sweep.py`) showed hillslope length —
and therefore RUSLE2 λ — swings ~2× across plausible values of these. The
slope-area diagnostic (`tests/_gary_slopearea.py`) on Gary's East found no
clear channel-initiation kink.

**Both results were measured on the 220 m-buffered DEM, and that is the
catch.** These thresholds are absolute *cell counts*, compared against an
accumulation value that is itself a truncation artifact:

1. **Truncation corrupts the area axis.** A field cell whose true catchment
   is 5 ac may register 2 ac on the buffered DEM. The slope-area scatter
   mixes fully-resolved cells with truncated ones, so the median-per-bin
   curve flattens and any kink smears out. The large-area bins that would
   show the channelized (down-sloping) limb are *absent* — the buffered DEM
   never accumulates that much area. "No kink on Gary's East" is not yet a
   finding about the terrain; it is a finding about the clip.
2. **Cell counts don't transfer.** "300 cells" means the same physical
   thing on two fields only if accumulation is physically real on both. On
   a buffered DEM it is not — it depends on how much of each field's
   catchment happened to fall inside 220 m.

Watershed-bounding does not pick the threshold. It removes the dominant
confound so the threshold question can be asked on honest data:

- **Accumulation becomes physical.** On a HUC-bounded DEM, accumulation at
  any cell is the real upslope contributing area. A threshold can then be
  stated in **physical units** — contributing area in acres, or a
  slope-area index — that transfer across fields.
- **The slope-area diagnostic becomes an honest experiment.** Re-running
  `_gary_slopearea.py` on watershed-bounded DEMs, the area axis is the true
  contributing area and the channelized limb is present. If a
  channel-initiation break exists it will show; if smooth tilled cropland
  genuinely has none — a real possibility for low-relief terrain — that is
  now a defensible finding rather than an artifact.
- **The delineated catchment gives a field-scale denominator.** The
  total contributing area lets thresholds be set relative to the catchment
  when no single absolute value transfers.

A slope-area *classifier* was proposed and rejected in the earlier flowline
session. This spec does not revive it as the answer. It re-positions it as
one diagnostic to re-run on clean data, paired with a convention-based
fallback (see "Threshold strategy"). The final default is deferred to the
diagnostic re-run — a build-time experiment, not a guess in this doc.

## Design

### Domain: a user-selected analysis polygon

The analysis boundary is **chosen by the user** in a map UI, not resolved
automatically. Three ways to set it:

- **HUC-12** — Django auto-resolves the field's HUC-12 from the ArcGIS WBD
  service and offers it as the default suggestion; the user accepts it.
- **USGS grid cell** — the user picks one or more 10 m DEM tiles.
- **Hand-drawn** — the user draws an arbitrary polygon: the HUC-12,
  something smaller, or anything in between.

Whatever the user settles on is the **literal compute domain**. The Lambda
sizes the DEM to that polygon's bounding box and computes on it directly —
it does not auto-extend or second-guess the drawing. The UI selection aids
(HUC-12 lines, USGS grid, a contour basemap — see "Selection UI") exist so
the user can place the boundary along a real ridge; the responsibility for
capturing the upslope contributing area sits with the user, informed by
those aids.

Per field, per flow-routed job:

1. **DEM domain = the selected polygon's bounding box.** Fetch and merge
   the USGS 10 m tiles covering it. The DEM is sized to the *bbox*, not
   masked to the polygon outline — clipping the raster to the outline would
   truncate flow at the boundary wherever the drawn/HUC line and the 10 m
   divide disagree. A selection of several HUC-12s or grid cells → the
   union bbox. (Plus the existing small edge buffer for `r.watershed` edge
   effects.)
2. **`r.fill.dir` + `r.watershed`** on that DEM → drainage, accumulation,
   L, LS, SPI, TCI. A HUC-12-sized domain is ~10⁶ cells at 10 m (tens of MB
   per array) — compute is cheap; the cost is tile I/O.
3. **Delineate the field's contributing catchment** within the domain — a
   reverse-D8 upslope flood-fill from the field cells (numpy, matching the
   existing `_divide_mask` / `_channel_heads` style in `grass_handler.py`),
   or `r.water.outlet` from the field's outlet cell. This is the *reported*
   watershed and yields the total contributing-area scalar that makes
   physical-unit thresholds possible. *(Deferred — pairs with the threshold
   experiment; not in the Lambda v1. See "Lambda side".)*
4. **Outputs clipped to field + 50 ft** for display — unchanged from today
   (`trim_and_post`, `_clip_wkt_to_cutline`). Compute big, clip small.

If the user draws a boundary smaller than the true contributing catchment,
flow near that edge is truncated — by their choice. The contour basemap is
the mitigation: it lets them see the terrain and draw along a divide. The
system does not silently correct an under-drawn boundary.

### Caveat — HUC-12 bounds sheet & rill correctly; draws on a main stem may not

WBD HUC-12s are delineated along drainage divides, but a HUC-12 can be a
drainage-area type with a stream flowing in from an upstream HUC-12. For
**sheet & rill** — the scope of this feature — the contributing hillslopes
are short and local and never cross a HUC-12 divide, so the HUC-12 is a
sound bound. Only large channelized main-stem flow could exceed it; that
affects the contributing-area number reported for a *draw*, not the
sheet/rill LS this feature exists to fix. Draws stay reported geometry,
not graded here.

### Sizing

Current Lambda (per `agkit.io-backend/tier2apps/topography/ENGINE.md`):
4096 MB / 900 s / 4 GB ephemeral `/tmp`. The computed arrays for a HUC-12
(~10⁶ cells) fit comfortably — memory is not the constraint. The real cost
is **DEM tile volume**: a HUC-12 bbox can span 1–4 USGS 1° tiles.
`ziphandler` clips tiles to the request extent on download, so `/tmp` holds
the clipped HUC-bbox DEM rather than full 1° tiles — but the build must
verify clip-on-download is driven by the HUC bbox, and watch the 4 GB
ephemeral headroom and 900 s timeout on fields whose HUC spans several
tiles. Raise limits only if a sweep shows it; do not over-provision
speculatively.

## Threshold strategy (after watershed-bounding)

Once flow runs on watershed-bounded DEMs, settle thresholds in this order.
This section is a *procedure*, not a verdict — the defaults are a
build-time experiment output, recorded back into this doc when known.

1. **Re-run the slope-area diagnostic.** Generalize `_gary_slopearea.py` to
   a small multi-field sample (varied relief), on watershed-bounded DEMs.
   The area axis is now true contributing area, with the channelized limb
   present.
2. **If a channel-initiation break is visible** — set `channel_threshold`
   (hillslope foot) and `draw_threshold` from it, expressed as a
   **contributing area in acres**, not cells. The Lambda converts acres →
   cells per-DEM from the actual cell size.
3. **If no break is visible** (plausible for smooth cropland) — fall back
   to a documented convention: an ephemeral-gully contributing-area
   threshold from the RUSLE2 / NRCS literature for `draw_threshold`, and
   the `r.watershed` stream-definition default for `channel_threshold`.
   Cite the source in code.
4. **Express thresholds in physical units in the payload** —
   `channel_threshold_area_ac`, `draw_threshold_area_ac` — converted to
   cells inside the Lambda. The cell-count options stay accepted for
   backward compatibility but are no longer the primary knob.
5. **Re-run the sensitivity sweep** (`_gary_sweep.py`) on watershed-bounded
   DEMs. If λ still swings more than ~1.3× across the plausible
   physical-unit range, that is a real finding about low-relief terrain —
   and it feeds the open question of whether RUSLE2 λ should come from
   terrain at all, or from the technician-drawn profile that
   `flowlines-feature.md` already names the authoritative override.

## Payload contract

New `metadata.watershed_boundary` field:

```jsonc
{
  "metadata": {
    "function_name": "watershed_length_slope_raster",
    "field_boundary": { "type": "FeatureCollection", "features": [ ... ] },
    "watershed_boundary": {            // NEW — null/absent if none selected
      "type": "FeatureCollection",
      "features": [
        {
          "type": "Feature",
          "properties": {
            "source": "hand_drawn",     // "huc12" | "usgs_grid" | "hand_drawn"
            "huc12": "071000050601"     // present only when source == "huc12"
          },
          "geometry": { "type": "Polygon", "coordinates": [ ... ] }
        }
      ]
    },
    "field_id": 123,
    "site_prefix": "agkit",
    "input_data": null
  },
  "post": { ... }
}
```

- `watershed_boundary` mirrors `field_boundary`: a FeatureCollection,
  EPSG:4326. One feature per selected polygon — a multi-HUC or
  multi-grid-cell selection carries several; the Lambda takes the union
  bounding box as the DEM domain. `source` records how it was chosen;
  `huc12` is present only on HUC-12 selections.
- **`null` or absent → graceful fallback.** The Lambda reverts to today's
  `gdal_bufferred_clip` 220 m buffer. A field with no saved selection,
  outside WBD coverage, or an ArcGIS suggestion that timed out, still
  produces flow output on the old domain — no SQS job is lost (per the
  no-retry semantics in `CLAUDE.md`, losing a job here would be silent, so
  the contract must never *require* the boundary).

## Django side — `agkit.io-backend/tier2apps/topography/` (pending)

- **Persist the selection.** A new model field on the topography side
  stores the user's chosen analysis polygon per field, with its `source`
  (`huc12` / `usgs_grid` / `hand_drawn`). Hand-drawn selections *must* be
  stored — they cannot be recomputed. This is a **migration**, and it
  reverses the earlier "transmitted, not stored" decision (which only held
  while the boundary was an auto-resolved HUC-12).
- **Selection API endpoint** — lets the map UI save, update, and clear a
  field's analysis polygon.
- **ArcGIS WBD client** — a new module that queries a WBD HUC-12
  FeatureServer (the USGS National Map hydro service, or a
  customer-specified ArcGIS endpoint — the URL is build-time config) by the
  field geometry, returning the polygon(s) + `huc12` code. Used to *suggest*
  the HUC-12 default and to feed the HUC-12 line layer in the UI. Uses
  `requests`, time-boxed; returns `None` on timeout or error.
- **`services.py:LambdaEventBuilder.build_event()`** — attach
  `watershed_boundary` to every job's `metadata` from the stored selection;
  if none is stored, fall back to the auto-resolved HUC-12 suggestion; if
  that also fails, attach `null` and log a warning.
- **Tests** — selection persists and round-trips through the API; event
  carries the stored selection; falls back to the HUC-12 suggestion when
  unset; `null` when both are unavailable; multi-polygon selection → union.

## Lambda side — `agkit.io-topography/app/` (built 2026-05-19)

The DEM-domain change is implemented and tested.

**Built:**

- **`geoworker.py` — DEM domain.** `build_common_cache` fetches the merged
  DEM once at the union of the two extents, then produces `clipped_raster`
  (field bbox + ~220 m, `_FALLBACK_BUFFER_DEG`) and `watershed_raster` (the
  selected analysis polygon's union bbox; aliases `clipped_raster` when no
  `watershed_boundary` is present). A `flow_domain` cache key records which
  domain was used (`"watershed"` / `"field_buffer"`).
- **Extent helpers** — `_watershed_extent` (parses the `watershed_boundary`
  FeatureCollection — dict or JSON-string — to a union bbox; `None` when
  absent), `_buffer_extent`, `_union_extent`.
- **`get_dem_raster_set` / `get_elev_raster`** are now extent-driven — tile
  selection and clip-on-download follow the analysis extent, not the field.
- **`gdal_bufferred_clip` → `gdal_clip_to_bounds`** — clips to an explicit
  bbox.
- **Routing** — `watershed_common` (the `watershed_*` jobs) and
  `mfd_flowlines` consume `watershed_raster`; `contour_lines`,
  `slope_public_10m`, `elev_public_10m`, `topo_blended_public_10m` stay on
  the field-buffered `clipped_raster`.
- **`null` / absent `watershed_boundary`** falls back to the 220 m buffer —
  the pre-watershed behavior — so no job is lost.
- **Tests** — `tests/test_watershed_domain.py` covers boundary parsing and
  the `build_common_cache` domain decisions (DEM fetch + GDAL clip mocked,
  so CI-runnable). The 52-test suite passes in the `agkit-topography` image.
- **Contract docs** — `agkit.io-topography/CLAUDE.md` and the "Buffer &
  clip" section of `flowlines-feature.md` updated.

**Deferred — pairs with the threshold experiment, not in v1:**

- Contributing-catchment delineation (Design step 3 — the reverse-D8
  flood-fill) and the contributing-area scalar.
- Physical-unit thresholds (`channel_threshold_area_ac` /
  `draw_threshold_area_ac` → cells) and folding in the hardcoded
  `get_watershed_maps(threshold=30)`. The cell-count options are unchanged.

These land with the "Threshold strategy" work, once the slope-area
diagnostic has been re-run on watershed-bounded DEMs.

## Selection UI — `agkit.io-erosion` (deferred, separate spec)

The analysis-area picker is a frontend feature of the **`agkit.io-erosion`**
daughter project. Its UX is **not yet spec'd** — deferred by the user as of
2026-05-19. Sketched here only so the cross-project contract is visible; the
picker's own spec lands later, in `agkit.io-erosion`.

When built, expect a MapLibre map centered on the field, with:

- **USGS grid boundaries** — the 10 m DEM tile footprint, so the user sees
  what data backs a selection.
- **HUC-12 lines** — from the ArcGIS WBD service; the recommended boundary
  to draw to or accept.
- **A contour tile basemap** — an existing public contour/topo tile service
  (USGS National Map topo, OpenTopoMap, or similar). It gives the user the
  terrain context to place a boundary along a real ridge, and is
  deliberately distinct from the field's own contour *vector* layer
  (`contour-feature.md`), which is also shown.
- **The field's contour vector lines** — the fine-grained field-scale
  contours from the existing contour job.
- **A draw tool** (GeoMan) for the hand-drawn option, plus click-to-select
  for the HUC-12 / grid-cell options.

The picker saves the chosen polygon through the Django selection API. It
never talks to the Lambda — the saved selection rides into the next event
build.

## What this feature deliberately does not do

- **No erosion grading.** Scope is watershed-correct flow geometry — L, LS,
  hillslopes, draws. Sheet vs rill grading is a downstream backend step
  composed from LS + soils (K) + weather (R); those factors are not in the
  Lambda.
- **Hand-drawn selections are persisted; the HUC-12 suggestion is not.** A
  user-drawn polygon is stored per field — it cannot be recomputed. The
  auto-resolved HUC-12 *suggestion* is still computed on demand, not cached;
  revisit only if ArcGIS latency forces it.
- **The contour basemap is an existing public tile service, not
  generated.** No tile-generation pipeline is in scope. If a public
  service's contour interval proves inadequate as a selection aid, a
  generated pyramid is a later, separate feature.
- **The Lambda does not auto-extend an under-drawn boundary.** The selected
  polygon is the literal compute domain; capturing the upslope contributing
  area is the user's call, supported by the UI aids.
- **No multi-HUC stitching beyond the bbox union.** A field straddling two
  HUC-12s uses the union bbox. Fields on a through-flowing main stem accept
  HUC-bounded draw contributing area (see caveat).
- **No new threshold default in this doc.** The default is the output of
  the build-time slope-area re-run, recorded back here when known.
- **No change to the postback contract or auth.** Same signed-URL
  postback; only the input *domain* changes.
- **LabCore / USGS2021 untouched** — this is the agkit.io SQS engine only.
