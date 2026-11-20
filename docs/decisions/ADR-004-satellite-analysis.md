# ADR-004: satellite analysis (Sentinel-2 NDVI)

- **Status:** Accepted
- **Date:** 2026-11-20 (decisions taken during Phase 3, 2026-10)
- **Deciders:** haruxvi

## Context

Phase 3 adds the first external data source: Sentinel-2 L2A imagery from the
Copernicus Data Space Ecosystem, through the Sentinel Hub APIs. From it the
system computes NDVI over a predio, per-lote statistics and anomalous zones
for an agronomist to review.

Constraints that shaped every decision:

- **Quota is finite.** Every Process or Statistical request spends
  Processing Units (PU) from a monthly budget. A wasted download is not just
  slow: it leaves the project unable to work for the rest of the month.
- **A round trip is slow.** 10 to 60 s per scene. It cannot happen inside an
  HTTP request.
- **The result must be correct without looking correct.** A badly scaled band
  or an interpolated class mask produces a plausible NDVI that is wrong, and
  nobody notices.
- **Anomalies are hypotheses.** The output feeds field decisions; it must say
  how much it can be trusted.

## Decision

### Asynchronous execution

`POST /predios/{id}/analyses` validates the request, inserts a `PENDING`
analysis, writes `ANALYSIS_REQUESTED` and enqueues a job on **arq** (Redis).
It answers 202 with the analysis id; the client polls.

- The worker (`backend/app/modules/analysis/worker.py`) runs at most
  `MAX_CONCURRENT_JOBS = 2` jobs: each holds rasters in memory.
- The job id is deterministic (`analysis:{id}`), so a second enqueue is a
  no-op.
- `requested_at` defaults to `clock_timestamp()`, the instant of each
  request even when several are created in one transaction; listings order
  by `requested_at DESC, id DESC`. `audit_log` keeps `now()` (KL-003).
- One analysis in flight per predio and type, enforced by the partial unique
  index `uq_analyses_one_active_per_type` (`status IN ('PENDING','RUNNING')`),
  not by an `if`.
- A periodic task (`expire_stale_analyses`, every 5 min) fails analyses stuck
  in `RUNNING` past `analysis_job_timeout_s` (`TIMEOUT`) or in `PENDING` past
  `analysis_pending_timeout_s` (`QUEUE_TIMEOUT`). Without it a dead worker
  would block the predio forever through the index above.
- The service has no knowledge of FastAPI or arq; both call into it.

### Storage abstraction

Rasters go through `StorageBackend` (`backend/app/shared/storage.py`), never
through `open()`. Keys are built only by `analysis_artifact_key` from UUIDs
and a fixed artifact name:

    rasters/{predio_id}/{analysis_id}/{ndvi.tif | preview.png | metadata.json}

so everything of a predio shares a prefix (bulk deletion, lifecycle rules).
`LocalStorageBackend` validates every key component and resolves every path
(symlinks included) inside its root; a key that escapes is refused.
`S3StorageBackend` is a stub and the application **refuses to start** with
`STORAGE_BACKEND=s3`: fail-closed rather than a half-working backend.
Production storage is open: **KL-005**.

Artifacts are served by ABAC-checked endpoints of the analysis module, never
by a static file route.

`preview.png` is not georeferenced: it shares the pixel grid of `ndvi.tif`
(EPSG:4326, the predio's bbox grown by two pixels). When the analysis
completes, the corners of that grid are stored in `analyses.preview_west`,
`preview_south`, `preview_east` and `preview_north`, and the detail exposes
them as `preview_bounds: [[south, west], [north, east]]`, the order a Leaflet
`imageOverlay` takes. They are computed once, never by opening the raster on
a GET. Four float columns rather than a JSONB, so a CHECK
(`ck_analyses_preview_bounds`) can require the box to be complete, inside
EPSG:4326 and not flipped. Analyses completed before the migration keep NULL
bounds.

### HTTP client on httpx, not sentinelhub-py

The client (`sentinel_client.py`) is written on `httpx` (async). What this
phase has to control is exactly what `sentinelhub-py` hides, and that library
is synchronous:

- OAuth token cached in Redis, refreshed by a single holder of a Redis lock;
  the others wait for it rather than stampeding the token endpoint.
- Two monthly budgets, both checked before **every** attempt: PU read from
  every response (`x-processingunits-spent`), and requests, counted before
  each attempt is sent. Calls are refused (`SENTINEL_QUOTA_EXCEEDED`, with
  `quota` saying which) once either is spent. See "Cost per analysis".
- Exponential backoff with jitter; `Retry-After` on 429 honoured, **in
  milliseconds** as Sentinel Hub sends it, capped.
- Circuit breaker in Redis (`sentinel_circuit_failure_threshold`,
  `sentinel_circuit_reset_s`); opening it writes `SENTINEL_CIRCUIT_OPEN`.
- Evalscripts are built from a band allowlist (`BAND_UNITS`): no caller string
  ever reaches one.
- The token and the client secret are never logged nor put in an exception.

`sentinelhub` was removed from `requirements.txt` (see
`docs/dependencies.md`).

### Scene selection in two steps

A Sentinel-2 tile is ~110 x 110 km; its cloud cover says almost nothing about
one predio. Selection therefore runs:

1. **Catalog API** (**not free**: 0.01 PU per request, measured): list the
   L2A acquisitions in the range over the predio's bbox; drop tiles that are
   100% cloudy. Only a cheap pre-filter.
2. **Statistical API**, one call for the whole range at
   `sentinel_cloud_stats_resolution_m` (60 m): the cloudy fraction (SCL 3, 8,
   9, 10) **over the predio's exact geometry**, per date, with `dataMask` as
   denominator, with `lastIntervalBehavior: SHORTEN` so the last day of the
   range is measured too (see "Validation against real data"). Cached in
   Redis (`sentinel_cloud_stats_cache_ttl_s`): past cloud cover does not
   change.
3. Pick the **most recent date** whose cloudy fraction over the predio is
   under `analysis_max_cloud_coverage` (30%). If none,
   `NO_SUITABLE_SCENE` with every evaluated date in `error_detail`.
4. **Process API**: download B04, B08 and SCL at full resolution (10 m) for
   that date only.

`NO_SUITABLE_SCENE` is a terminal state distinct from `FAILED`: a cloudy month
is a condition of the world, and mixing it with system errors would hide real
failures behind bad weather. The same state is used when the downloaded scene
has less than `analysis_min_valid_pixels_ratio` (60%) of valid pixels.

### Scaling delegated to Sentinel Hub, and the 04.00 offset

Since processing baseline **04.00** (January 2022) L2A digital numbers carry
an offset (`BOA_ADD_OFFSET = -1000`) besides the 1/10000 factor. Ignoring it
yields an NDVI that is wrong without ever looking wrong.

The decision is to **not** scale locally. Bands are requested in
`REFLECTANCE` units with `harmonizeValues: true`, so Sentinel Hub applies the
factor and removes the offset server-side, for scenes before and after the
baseline change alike. `REFLECTANCE_SCALE_FACTOR` is therefore an explicit
`1.0`, kept visible so nobody "fixes" it into a second scaling.

**Verified on real data (2026-11-20, baseline N0513).** On Copernicus Data
Space the values arrive already without the offset: B04 requested in `DN`
equals exactly 10000 × the `REFLECTANCE` value, and its 1st percentile is
209 DN, impossible if the +1000 offset were present. Contrary to what this
ADR first assumed, `harmonizeValues` made **no observable difference** on
that scene (`true` and `false` gave identical pixels). The request keeps it
`true`, which is the documented default, but correctness does not rest on it.
What would expose an unremoved offset is the percentile record below: with
the offset, no pixel's red reflectance could fall under 0.1.

#### What protects against which scaling error

Three controls, each for a different failure; none of them is a heuristic on
the values of a scene:

1. **Unscaled values** (DN integers in the thousands): `check_reflectance`
   refuses a band with more than 1% of its valid pixels outside `[0, 1.6]`
   (`ReflectanceScaleError`, the analysis ends `FAILED` with
   `REFLECTANCE_SCALE`). This guard **does not** see a +0.1 shift: shifted
   reflectance is still inside the range.
2. **The offset**: covered by the request, deterministically and without
   spending quota. `test_fetch_bands_asks_for_harmonised_reflectance_and_nearest_scl`
   fixes that the Process request asks for `REFLECTANCE` units and
   `processing.harmonizeValues: true`, the parameters the documentation
   specifies for removing the baseline 04.00 offset. If anyone removes them,
   CI fails before a single NDVI is computed.
3. **Evidence for later review**: the B04/B08 percentiles (p1, p50, p99) of
   every completed analysis are in `ANALYSIS_COMPLETED`. A shift would show
   as both bands about 0.1 higher, and a red p1 that never goes under 0.1.

A threshold on those percentiles was rejected: a predio of bare soil in
summer has a legitimately high red band and would raise false alarms.

How it was verified on 2026-11-20: the same scene requested in `DN` gave
exactly 10000 × the `REFLECTANCE` value (median difference 0.0), with a B04
1st percentile of 209 DN, that is, 0.021 in reflectance; with the offset
present no pixel could fall under 1000 DN (0.1).

### SCL masking and nearest neighbour

Pixels are masked by the Scene Classification Layer before computing NDVI.
Excluded classes: 0 (no data), 1 (saturated/defective), 3 (cloud shadow), 8
and 9 (cloud), 10 (thin cirrus), 11 (snow/ice), plus `dataMask = 0`. Class 6
(water) is excluded by default (`analysis_exclude_water`): a pond would drag
every statistic towards negative values.

SCL is native at 20 m and the bands at 10 m. It is resampled **only by nearest
neighbour**: the Process request declares `upsampling` and `downsampling`
`NEAREST` explicitly, and `resample_nearest` exists, tested, for SCL served at
native resolution. Interpolating a class raster invents classes: the average
of 4 (vegetation) and 8 (cloud) is 6 (water), which is nonsense.

NDVI is computed in float32; a near-zero denominator marks the pixel invalid
instead of dividing. The result is clipped to the exact predio geometry and
stored as a float32 GeoTIFF with explicit NaN nodata.

### Per-lote statistics, greenhouses and infrastructure

Statistics (mean, median, std, min, max, p10, p90) are computed per lote, only
from pixels whose centre lies in the lote. A lote gets values only with
**both** `analysis_lote_min_valid_pixels` (20 px, 0.2 ha) and
`analysis_lote_min_valid_ratio` (50%) of itself visible; otherwise
`INSUFFICIENT_PIXELS`.

Some lote types are **counted but never measured**, and therefore get no
anomalies (a lote without statistics has nothing to compare its pixels to).
They are declared once, in `NOT_MEASURED_LOTE_TYPES`
(`backend/app/modules/analysis/ndvi.py`), never as separate `if`s:

- `INVERNADERO` → `NOT_APPLICABLE_GREENHOUSE`: the satellite sees the
  plastic roof, not the crop.
- `BODEGA_AREA` → `NOT_APPLICABLE_INFRASTRUCTURE`: roofs and yards. Their
  NDVI is noise, and a roof next to green rows reads as a low-NDVI anomaly.
  Found on real data: the demo "Bodega de insumos" got statistics and was
  eligible for anomalies before this exclusion.

They still appear in the preview, which shows the NDVI of the scene, not a
judgement on the lote.

An optional inner buffer (`analysis_lote_inner_buffer_m`, off by default)
drops mixed border pixels; it stays off until real data shows how it interacts
with the thresholds.

### Anomalies: spatial z-score within each lote

For each measured lote:

    z = (ndvi - lote_mean) / lote_std

Pixels with `z <= anomaly_zscore_threshold` (-2) are grouped by
8-connectivity (`scipy.ndimage.label`); clusters under
`anomaly_min_cluster_pixels` (10) are dropped; the rest are vectorised,
simplified (`anomaly_simplify_tolerance_m`) and clipped to the lote.

- **Spatial, not temporal**, because it works from the first analysis; a
  temporal method needs history that does not exist yet. Methods share the
  `AnomalyMethod` protocol so a temporal one can be added without rewriting.
- **Within a lote, never across lotes**: varieties, ages and management give
  different baselines.
- A lote with `std < 1e-6` is uniform and has no anomalies.
- The method assumes a unimodal distribution and the anomaly inflates the
  spread it is measured against: a zone covering more than ~20% of its lote
  cannot reach z = -2. Both limits are documented in **KL-006**.

Every anomaly is a **hypothesis**. It carries `review_status`
(`CONFIRMED`, `DISMISSED`, `NEEDS_FIELD_CHECK`), set by an agronomist and
audited (`ANOMALY_REVIEWED`).

### Severity cuts: provisional, with a calibration path

    strong = mean_z <= anomaly_severity_high_zscore        (-3)
    large  = area_m2 >= anomaly_severity_high_area_m2      (5 000 m2)
             or area_ratio >= anomaly_severity_high_area_ratio  (10%)
    HIGH if strong and large; MEDIUM if only one; LOW otherwise

"Large" is absolute **or** relative on purpose: 0.5 ha is 1% of a 50 ha
paddock but half of a 1 ha block.

These cuts are **not validated agronomically**. With the default values the
relative condition combined with z <= -3 is only met at the exact limit (see
KL-006), so HIGH arrives in practice through absolute area.

Calibration path: reviewed anomalies are the labelled data. Once a few
hundred carry a `review_status`, compare the distribution of `mean_zscore`,
`area_m2` and `area_ratio_of_lote` between `CONFIRMED` and `DISMISSED`, and
move the cuts (they are settings, no code change). Until then the frontend
must present severity as an ordering aid, not a diagnosis.

### Idempotency, `DUPLICATE_SCENE` and `superseded_by`

- `run()` only runs a `PENDING` analysis; a retried job that finds it in any
  other state returns at once. Recomputing costs quota.
- After selecting the scene and **before downloading anything**, the service
  looks for a vigente `COMPLETED` analysis of the same predio, type and
  `scene_id`. If there is one and the request is not `force`, the new
  analysis ends as `DUPLICATE_SCENE` with `resolved_to_analysis_id` pointing
  at it (`ANALYSIS_RESOLVED_TO_EXISTING`). Not an error: the result exists,
  and only the selection PU were spent. A CHECK ties the state and the
  pointer together.
- The partial unique index `uq_analyses_completed_scene` allows one vigente
  result per scene (`status = 'COMPLETED' AND superseded_by_analysis_id IS
  NULL`).
- With `force`, the scene is recomputed. At completion the previous vigente
  result is locked and marked `superseded_by_analysis_id` (flushed before the
  new one turns `COMPLETED`, so the index never sees two), and
  `ANALYSIS_FORCED_RECOMPUTE` (WARNING) records the superseded id and how many
  reviewed anomalies stay behind on it. The previous analysis is **never
  deleted**; its raster and reviews remain. Reviews are not transferred:
  **KL-007**.
- History per lote (`/predios/{predio_id}/lotes/{lote_id}/history`) uses
  vigente analyses only.

### Rejected or expired credentials

A client secret on Copernicus Data Space expires. When the identity service
refuses the credentials (401 or 403, or 400 with `invalid_client` or
`unauthorized_client`; only that field of the body is read, against an
allowlist), the client raises `SentinelCredentialsRejectedError` and the
analysis ends `FAILED` with `error_code = SENTINEL_CREDENTIALS_REJECTED` and a
message saying the credentials are invalid or expired and how to renew them.
It is not retried and **never counts against the circuit breaker**: an
expired secret is not Copernicus being down. A 5xx or a transport error from
the identity service is a different case (`SENTINEL_AUTH`, reason
`TOKEN_UNAVAILABLE`), and missing credentials have their own reason
(`NOT_CONFIGURED`).

### Validation against real data

Run on 2026-11-20 over a development predio in Colchagua (49 ha, invented
boundary), range 2026-10-21 to 2026-11-20:

- Catalog: 6 acquisitions, 0.01 PU. Statistical API over the predio,
  2026-10-24 to 2026-11-13: **0.025 PU**. Three dates were 100% cloudy over
  the predio, 2026-11-03 had 9.6%, and 2026-11-08 had 0%, which was chosen.
  **2026-11-13 was newer and also 0%, but was never measured: a bug.** The
  range ends at 23:59:59, one second short of a whole day, and the
  Statistical API's default (`lastIntervalBehavior: SKIP`) drops an
  incomplete last interval, so the newest date of any range could never be
  chosen. Fixed with `lastIntervalBehavior: SHORTEN`, verified against the
  real API (2026-11-13 then comes back, 0% cloud), and the cloud cache key was
  versioned (`v2`) so measurements cached without the last day are not
  reused.
- Full run through the real worker: `COMPLETED` in 2.8 s, 0.059 PU (Catalog
  again plus the Process request; cloud statistics came from the cache),
  100% valid pixels, four lotes measured, three LOW anomalies. After the fix,
  the same range chose 2026-11-13.
- Reflectance percentiles: B04 p1/p50/p99 = 0.021 / 0.069 / 0.187, B08 =
  0.164 / 0.254 / 0.581. NDVI median 0.57. Coherent for vineyards in spring.
- The preview, read back from storage, shows field boundaries and bare plots
  where expected, and is transparent outside the predio.

### Cost per analysis (measured) and the two budgets

For a 49 ha predio, on 2026-11-20:

| Request | PU | Requests |
|---|---|---|
| OAuth token (only when the cached one expired, ~9 min TTL) | 0 | 1, counted as a precaution |
| Catalog | 0.01 | 1 |
| Statistical API, 21 days at 60 m | 0.025 (one day with `SHORTEN`: 0.01) | 1 |
| Process API, B04 + B08 + SCL at 10 m | 0.0495 | 1 |
| **Total per analysis without cache** | **about 0.085** | **3, or 4 with a token** |

A `DUPLICATE_SCENE` costs only the selection (about 0.035 PU, 2 requests); a
repeated selection within the 6 h cloud cache saves the Statistical request.
The Process request's PU scale with the predio's area; its request count
does not. Retries count as requests too.

**The quota of the account** (Copernicus dashboard, 2026-11-20): 30 000 PU
and 30 000 requests per month, overage 0: when either runs out the API stops
answering until it resets. The public documentation lists 10 000 and 10 000
for a general user; the dashboard is what applies to this account.

**Which runs out first.** With 30 000 of each: 30 000 / 0.085 ≈ 350 000
analyses by PU, against 30 000 / 3 = 10 000 by requests. **Requests run out
about 35 times sooner**, so they are counted too (`sh:requests:{YYYY-MM}` in
Redis, beside `sh:pu:{YYYY-MM}`), and both are checked before every attempt.

**Budgets at 80% of the quota**: `sentinel_monthly_pu_budget = 24000` and
`sentinel_monthly_request_budget = 24000`. A budget equal to the quota leaves
no margin to react before the API itself refuses.

**What is counted as a request.** Copernicus does not document whether the
OAuth token request counts against the quota, nor whether failed requests
do. Both are counted: every attempt (failed, retried or not) and every token
request, before it is sent. Stopping a little early is better than being cut
off in the middle of an analysis.

**The monthly reset.** The documentation says quotas "reset on the first day
of every month", without a time or a timezone. The counters' month is
computed in **UTC** (`_month()` in `sentinel_client.py`; a test fixes that
23:30 on November 30 in Chile is already December). If Copernicus resets at
another hour, the two disagree for a few hours a month. The 20% margin
covers it as long as this system is the only user of the account: since it
stops at 80%, the real counter is at most at 80% when ours resets, so those
hours would have to consume more than 20% of the monthly quota (6 000
requests) for the API to cut off.

### Concurrent connections

The Copernicus FAQ gives **4 simultaneous connections** for a Copernicus
General account. Nothing limited it before: two jobs in parallel, or two
worker processes, could exceed it, and the refusals would look like a
failing service.

`sentinel_max_concurrent_requests = 3`: one under the limit, left for the
dashboard or a manual test. The limit is **global, not per process**: a
Redis sorted set (`sh:connections`) holds one member per request in flight,
scored by the expiry of its lease; a Lua script, using the Redis server's
clock so every worker shares it, drops expired leases and admits a request
only while fewer than 3 are held. A worker that dies holding a slot frees it
when the lease ends (`sentinel_request_timeout_s` + 30 s). An in-memory
semaphore would let each worker through on its own.

Every HTTP request takes a slot, the token request included, and is counted
against the request budget only once it holds one. With no free slot a
request **waits**, polling, up to `sentinel_concurrency_wait_s` (120 s); then
it fails with `SentinelConcurrencyTimeoutError` (`error_code =
SENTINEL_BUSY`), which is our own limit and never counts against the circuit
breaker.

The account's quota turned out to differ from the public documentation
(30 000 PU and requests on the dashboard, 10 000 in the docs), so the real
connection limit may differ from the FAQ too. 3 stays: if the real limit is
higher, only some parallelism is lost; if it is 4 and more were used, the
refusals would look like a failing service.

### Attribution

Products derived from Sentinel data must credit the source: "Contains
modified Copernicus Sentinel data [year]". The year is the scene's.

- The analysis detail carries it ready to show, as `attribution`, for
  analyses with a result (`COMPLETED`, and `DUPLICATE_SCENE`, which points
  at one); it is null otherwise. The frontend never builds it.
- The GeoTIFF carries it inside the file: as the TIFF Copyright tag
  (`TIFFTAG_COPYRIGHT`), which common GIS tools display, plus `ATTRIBUTION`
  and `DATA_SOURCE` metadata. `metadata.json` carries it too.

**Rule for later phases:** every place that shows a satellite result (map,
PDF report, export of any kind) must show the attribution. The frontend and
reporting phases implement it; the rule is written here so none of them
ships without it.

### Quota monitoring (provisional)

`/api/v1/analysis/status` exposes this month's PU and requests against
their budgets, and is restricted to AUDITOR for now. That is provisional:
the Copernicus quota is a metric of the platform, not data of a predio, and
AUDITOR is a data-inspection role. When observability exists (metrics and
alerts), quota consumption moves there, with an alert at 80% of each budget.

### Credentials and tests

No test may reach the real API. `backend/tests/conftest.py` blanks
`SENTINEL_CLIENT_ID`, `SENTINEL_CLIENT_SECRET`, `SH_CLIENT_ID` and
`SH_CLIENT_SECRET` before any settings are built (environment variables
outrank `.env`), and `backend/tests/test_sentinel_guard.py` fails with an
actionable message when any of them was present in the process environment.
Every Sentinel fixture is synthetic (`backend/tests/fixtures/sentinel/`).

## Alternatives considered

1. **Synchronous analysis inside the request.** Rejected: 10-60 s per call
   would stall the API with a handful of users.
2. **sentinelhub-py.** Rejected: synchronous, and hides token caching, PU
   accounting, retries and the breaker that this phase must own.
3. **Choosing the scene by tile cloud cover.** Rejected: a tile at 12% can
   cover the predio entirely; the case is in the fixtures (2026-09-20).
4. **Downloading every candidate date at full resolution and choosing
   locally.** Rejected: multiplies PU by the number of dates.
5. **Local scaling (DN / 10000 + offset).** Rejected: depends on the
   baseline of each scene, which is easy to get wrong silently; Sentinel Hub
   already harmonises.
6. **Fixed NDVI thresholds for anomalies.** Rejected: baseline NDVI depends on
   crop, age and season; a fixed cut flags whole lotes or none.
7. **Deleting the previous result on a forced recompute.** Rejected: it
   carries reviews and has been seen by people; the audit trail points at it.

## Consequences

- The worker is a second process to deploy (`worker` service in
  `docker-compose.yml`), sharing the raster volume with the API.
- Without Sentinel credentials the analysis endpoints report themselves
  unavailable; the rest of the application runs.
- The frontend overlays `preview.png` with `preview_bounds`; it never needs
  to read the GeoTIFF.
- Severity and anomalies must be shown as hypotheses pending review.
- Local storage is not production storage (KL-005); the z-score has known
  blind spots (KL-006); forced recomputes accumulate (KL-007); the free
  quota has no availability guarantee and one account serves development and
  production (KL-008).

## Related limitations

- [KL-005](../KNOWN-LIMITATIONS.md#kl-005--el-almacenamiento-local-de-rasters-no-es-apto-para-producción):
  local raster storage is not fit for production.
- [KL-006](../KNOWN-LIMITATIONS.md#kl-006--la-detección-de-anomalías-asume-una-distribución-unimodal-dentro-del-lote):
  the anomaly detection assumes a unimodal distribution within the lote.
- [KL-007](../KNOWN-LIMITATIONS.md#kl-007--los-análisis-reemplazados-por-un-recómputo-forzado-se-acumulan-y-sus-revisiones-quedan-dispersas):
  superseded analyses accumulate and their reviews stay scattered.
- [KL-008](../KNOWN-LIMITATIONS.md#kl-008--el-análisis-satelital-depende-de-una-cuota-gratuita-sin-garantía-de-disponibilidad):
  the free Copernicus quota has no availability guarantee; a single account
  serves development and production.
