# agkit.io-topography — wishlist / roadmap

Captured 2026-05-13 from an ideation pass after the initial scaffold landed.
Not committed to scope or priority; this is a backlog to draw from.

## Companion specs (graduated from wishlist)

These have moved out of "ideation" and into their own feature docs. Items
here are no longer candidates for re-discussion — go to the spec.

- **`contour-feature.md`** — 2 ft / 10 ft index contours over the field +
  50 ft buffer. Feeds the erosion app's slope-shooting basemap.
  **Shipped** — `contour_lines` in `app/geoworker.py`.
- **`flowlines-feature.md`** — MFD-extracted flow paths with per-line
  catchment polygons and Douglas-Peucker'd slope profiles. Feeds the
  erosion app's auto-suggest layer (planner-drawn is the override path).
  Has hard prerequisites in this wishlist — see notes on A2 and A4.
  **Shipped** — `mfd_flowlines` in `app/geoworker.py`.
- **`blended-topo-feature.md`** — multidirectional hillshade composited
  over a color-relief elevation map ("USGS 10m topography layer"). A
  basemap layer, not a wishlist graduate — documented here for parity.
  **Shipped** — `topo_blended_public_10m` in `app/geoworker.py`.

## Framing

Two things temper the urgency of several "correctness" items below:

1. **Field + buffer remains the default extent.** HUC-bounded mode (A3) is
   opt-in, not a replacement for the current per-field DEM fetch.
2. **The scalar L / LS reductions are operator-review starting points,** not
   authoritative RUSLE2 1D-profile inputs. That makes the A-factor and the
   raster LS more important than the scalar reduction (A6).

## Tier A — correctness improvements to LS / L computation

Together these get the LS raster closer to a RUSLE-ready surface. Per the
operator-review framing above, A1 + A2 + A4 + A5 are the highest-value subset.

### A1. Replace `slope_steepness` raster with Desmet & Govers (1996) LS
r.watershed's native `slope_steepness` isn't the LS formulation most
raster-RUSLE users expect. Pipeline:
* r.watershed (MFD) → `accumulation` raster.
* LS = `((accumulation × cellsize / 22.13)^m × (sin(slope) / 0.0896)^n)` per
  pixel, with `m ≈ 0.4–0.6`, `n ≈ 1.3` as typical defaults.
* Rename the exported parameter — e.g., `ls_factor_dg` — to signal the
  formulation explicitly.
* Keep r.watershed's native `slope_steepness` available as a diagnostic
  layer, not the canonical LS.

Coupled backend change: new constant in `tier2apps/topography/schema.py`.

### A2. Force MFD on r.watershed
Today: `flags="b"`. Switch to `flags="bm"` (verify flag syntax in GRASS 8;
the `m` flag toggle may have moved). MFD concentrates less flow into thin
lines and gives smoother, more realistic accumulation on hillslopes — the
right default for 10 m DEMs on single ag fields.

**Now a blocker for `flowlines-feature.md`** — the MFD flow-line extractor
calls `r.watershed -m` explicitly. Either land A2 first, or have the
flow-lines handler set the `-m` flag locally and accept the temporary
divergence from the canonical pipeline. The former is cheaper.

### A3. (Optional) HUC12 enclosing-watershed mode
**Default stays field + buffer.** When the event opts in via
`metadata.huc_mode=true`:
1. Look up the HUC12 the field sits in (HUC10 if near a HUC12 divide).
2. Pull the DEM clipped to that polygon.
3. Run the whole pipeline on the HUC12 extent.
4. Clip outputs to the field polygon at the very end.

Eliminates artificial-divide artifacts at the square buffer edge and the
degraded outermost rows from r.watershed. HUC12s are ~40 km² so the 10 m
raster is small.

Lookup source — HUC12 ArcGIS FeatureServer (point-in-polygon by field
centroid; supports a standard ?geometry=… spatial query):
```
https://services5.arcgis.com/7weheFjxuNkGGiZi/arcgis/rest/services/Watershed_Boundary_HUC12/FeatureServer
```
Alternative: WBD (Watershed Boundary Dataset) layers on `prd-tnm` for bulk
or offline use.

### A4. Hydro-condition the DEM before r.watershed
Today: nothing. Add a breach step between the clipped DEM and r.watershed.
Breach over fill is preferred for ag landscapes (culverts, road crossings,
ditches). Tooling options:
* GRASS `r.hydrodem`.
* WhiteboxTools `BreachDepressions` (recommended; adds a binary dep).

**Quality multiplier for `flowlines-feature.md`.** The flow-lines spec
uses `r.fill.dir` as a stopgap; breach produces visibly better paths in
ag terrain (no dead-ends at road culverts). Not a blocker — flow lines
ship correctly without it — but the planner-facing output gets
noticeably better the day A4 lands.

### A5. Slope-length cap
RUSLE's empirical basis breaks past ~100–120 m of slope length. Flow-
accumulation LS produces huge values in convergent hollows where deposition
would actually occur. Two masks to apply together:
* Cap LS at a configurable max (e.g., 30).
* Use r.watershed's `stream` output to mask channel cells out of LS — they
  shouldn't have an LS value at all.

### A6. (Lower priority) Reconsider the scalar reduction for L / LS
Today's `np.nanmean` over the field polygon is a usable operator-review
value but not a RUSLE2-1D-profile input. If the erosion daughter ever
demands tighter inputs, switch to a principal-flow-path reduction (longest
accumulating flow path inside the polygon → length + average steepness).
Until then, keep `nanmean` and document it as a starting-point summary.

### A7. Edge-cell exclusion
Drop r.watershed's outermost 1–2 pixel rings from any field-level reduction
— flow accumulation at the edges is degraded regardless of algorithm.
Subsumed by A3 when HUC mode is on; explicit otherwise.

## Tier B — adjacencies that compound with Tier A

### B1. 1 m 3DEP lidar support where coverage exists
USGS 3DEP has 1 m lidar DEMs for a growing share of CONUS. Resolves micro-
topography 10 m can't (terraces, waterways, road crossings — i.e., the
conservation structures Tier A indirectly cares about). New dispatch
surface: `elev_public_1m`, `slope_public_1m`, `ls_factor_dg_1m`. Fall back
to 10 m where 1 m unavailable.

### B2. Optional stream-burning of known conservation structures
If the event carries `metadata.input_data.burn_lines` (GeoJSON of terraces
/ waterways / ditches), `r.carve` them into the DEM before r.watershed.
Otherwise produce the "as-if-bare" baseline. Django growing a
`TopographyOptions` model would feed this.

### B3. TWI (Topographic Wetness Index)
`ln(accumulation × cellsize / tan(slope))`. Falls out for free once MFD
accumulation is in hand. Useful for hydric-soils mapping and irrigation
planning. New named function: `watershed_twi`.

### B4. Per-pixel contributing-length and slope outputs
For erosion or any downstream model that wants alternative LS formulations
or its own routing. Export `contributing_length_m` and `slope_pct` rasters
alongside the canonical LS.

### B5. Plan and profile curvature
`r.slope.aspect` emits these alongside slope. Useful for distinguishing
concave (deposition) vs convex (transport) pixels — conceptually what
RUSLE2's profile routing approximates.

## Tier C — operational / reliability

Orthogonal to the geo improvements. Tier A raises *what* we compute; Tier C
raises *how reliably* it runs.

### C1. Per-job failure → SQS DLQ, not silent `"ERROR"`
Today `process_payload` catches a job exception, returns `"ERROR"` as a
string, SQS deletes the message, Django never hears about the failure.
Options:
* Re-raise from `handler()` so the messageId lands in `batchItemFailures`.
* Or POST a "job failed" scalar back to Django for visibility.

### C2. Idempotency token
SQS is at-least-once. Cache `(field_id, function_name, job_token)` in a
small DynamoDB table with a 24 h TTL on first successful POST; short-
circuit redeliveries. Saves the compute, not just the storage.

### C3. r.watershed threshold scaled to field area
Hardcoded `threshold=30` (pixels) means "stream" is defined wildly
differently for a 1-acre field vs a 200-acre field. Tie threshold to a
fraction of pixels-inside-the-polygon (e.g., 1–3%).

### C4. Structured logging + per-job timing
Switch `print()` → `logging` with a JSON formatter. Emit `phase`,
`field_id`, `elapsed_ms` per step. Unblocks CloudWatch Insights queries by
field_id, plus P50 / P95 r.watershed time by field size.

### C5. Postback retry without re-running compute
Failure mode today: Django's postback hiccups → SQS redelivers → GRASS
runs again. Cache produced artifact in `/tmp` by
`(field_id, function_name, job_token)`. On redelivery, skip straight to
POST if artifact is still on disk and fresh.

### C6. Validate `post_url` host against an allowlist
`ALLOWED_POSTBACK_HOSTS` env var. Defense against confused-deputy POSTs if
a malformed/replayed event ever reaches the queue.

### C7. Warm-container and parallelism wins
* Keep `/tmp/dem/` tile cache across warm invocations (today wiped after
  every call).
* Parallel DEM tile fetches via threadpool.
* Cache the projected DEM (`proj_buffered_dem`) so `slope_public_10m`
  doesn't recompute it on every invocation.
* `gdal.Warp` mosaic → GDAL VRT for cross-tile fields (near-instant, lazy).

### C8. CloudWatch EMF metrics + X-Ray tracing
Custom metrics: `jobs_completed`, `jobs_failed`,
`r_watershed_seconds{size_bucket}`. X-Ray spans for
SQS → Lambda → GDAL → GRASS → POST.

### C9. CI on GitHub Actions
Build image, run unittest inside it, push to ECR on tag.

### C10. CLI runner
`uv run python -m app tests/fixtures/sqs_event.json` for local debugging
without docker.

## Architectural shifts (bigger; not near-term)

* **One SQS message per job, not per bundle.** Failure isolation + Lambda-
  concurrency parallelism. Costs the GRASS-bundle shared cache across the
  6 watershed jobs.
* **Step Functions with a Map state** if product count grows past ~10.
* **EFS for cross-Lambda DEM tile cache.** Eliminates re-fetch when many
  fields in the same tile area get processed back-to-back.
* **Provisioned concurrency** for cold-start-sensitive flows. GRASS + GDAL
  boot is 3–5 s.

## Out of scope (tracked elsewhere)

* Backend `services.py` SQS migration (replaces the S3 upload leg).
* Backend `schema.py` constants for new raster params (`ls_factor_dg`,
  `watershed_twi`, etc.).
* IAM execution role + SQS queue + event-source mapping with
  `FunctionResponseTypes=["ReportBatchItemFailures"]`.

## Suggested sequencing

1. **Sprint 1 — correctness:** A1 + A2 + A4 + A5. Smallest scope that
   produces a RUSLE-ready raster LS. A2 also unblocks
   `flowlines-feature.md`; A4 then upgrades flow-line quality.
2. **Sprint 2 — ops baseline:** C1 + C2 + C4. DLQ-on-failure + idempotency
   + structured logs. Prerequisites for trusting Sprint 1 in production.
3. **Sprint 3 — extent (optional):** A3 + A7. HUC12 mode behind a flag.
4. **Sprint 4 — postback robustness:** C5 + C6.
5. **Later, as demanded:** A6 (only if the erosion daughter pushes for
   it), Tier B surfaces, architectural shifts.
