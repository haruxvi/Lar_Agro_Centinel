# ADR-005: device registry and drone capture ingestion

- **Status:** Accepted, in progress. Phase 4 block 1 fixes the decisions and
  the configuration below; later blocks add how each one is implemented.
- **Date:** 2026-11-20
- **Deciders:** haruxvi

## Context

Phase 4 adds the second data source: aerial photos taken with a drone. The
target drone is a consumer model (SJRC F11S 4K PRO) without an open SDK, so
ingestion is by manual upload: an operator flies with the manufacturer's app,
takes out the microSD card and uploads the photos.

This is the largest attack surface of the system so far: **binary files
supplied by users**. A JPEG can be a decompression bomb, a polyglot (a valid
JPEG that is also HTML or ZIP), carry hostile EXIF, or arrive under a name
built to escape a directory. Photos also carry precise coordinates of the
predio and the device's serial number, so they are personal and commercial
data, and the original file is evidence in a chain of custody.

## Decision

The following decisions are closed; the phase implements them.

### A. A capture session is bound to one predio

A session (one flight) is opened **on a predio**, through
`require_predio_access`, and photos are uploaded inside it. GPS is used only
within that predio: to find the lote, and to flag what fell outside.

The alternative, uploading loose photos and finding the predio from each
photo's GPS, would use `find_predios_containing_point`, which returns every
predio at that point regardless of its owner: a photo falling on someone
else's predio would reveal that it exists. With the session bound to an
accessible predio there is no path by which a user learns anything about a
predio they cannot access. No spatial query in this phase may run outside the
session's predio.

### B. JPEG only, recognised by its magic bytes; no ZIP

Only JPEG is accepted in this phase, recognised by its first bytes
(`FF D8 FF`), never by the extension or by the `Content-Type` the client
sends. No RAW/DNG, HEIC, PNG or containers.

No ZIP on purpose: it brings two whole classes of vulnerability (zip bombs
and zip slip) in exchange for sparing the user a multiple file selection.

### C. Light validation in the request, decoding in the worker

The upload endpoint only checks size (while reading), magic bytes and the
hash. Full decoding with Pillow, where image-decoder vulnerabilities live,
runs in the arq worker, isolated from the process that serves HTTP, under a
pixel limit whose warning is turned into an error (Pillow only fails at twice
the limit, and below that it decodes whatever dimensions a file declares).

### D. The original file is immutable

The original is stored exactly as it arrived, with its SHA-256 computed while
receiving it. It is never modified, recompressed, or stripped of EXIF: it is
evidence in a chain of custody. Everything derived (thumbnail, metadata) is
stored separately. The worker recomputes the hash before processing; a
mismatch is an incident (`CAPTURE_INTEGRITY_FAILURE`, CRITICAL), not an
error to retry.

### E. Thumbnails are re-encoded from scratch, without EXIF

Re-encoding from decoded pixels neutralises polyglot files; dropping EXIF
keeps a shared thumbnail from leaking coordinates or the serial number. The
EXIF orientation is applied before scaling, so the thumbnail is upright.

### F. "No GPS" and "outside the predio" are not errors

They are states asking for a human action (`NO_GPS`, `OUT_OF_BOUNDS`), like
`NO_SUITABLE_SCENE` in Phase 3, and are never mixed with `REJECTED`. A photo
without coordinates is a valid photo that someone has to place by hand.

### G. No XMP in this phase

The height above take-off that DJI drones write lives in XMP. Parsing
untrusted XML safely needs `defusedxml` (a new dependency) against XXE and
billion-laughs attacks. It stays an extension point of the DJI adapter; the
consequence for altitude is a known limitation (Phase 6 must decide how to
verify height above ground).

### H. API prefix

Every endpoint is under `/api/v1/`.

### Timestamps and ordering of captures

- `captures.uploaded_at` defaults to **`clock_timestamp()`**, not `now()`,
  from the initial migration. The photos of one upload request share a
  transaction, and `now()` is the start of the transaction: every photo of a
  request would get the same instant. Same lesson as `analyses.requested_at`
  (see the note in KL-003); `audit_log` keeps `now()` until Phase 7.
- Capture listings are ordered by **`captured_at`, with `id` as the
  tie-breaker**, so pagination is deterministic even when a burst of photos
  shares a second, or `captured_at` is missing.

### Configuration

Added to `backend/app/shared/config.py` (all overridable by environment):

| Setting | Default | Why |
|---|---|---|
| `captures_max_file_size_mb` | 40 | Enforced while streaming each file; a 4K JPEG is 5 to 10 MB |
| `captures_max_files_per_request` | 20 | Checked before reading any file |
| `captures_max_files_per_session` | 2 000 | A flight is hundreds of photos |
| `captures_upload_rate_limit` | 60/minute | Per the upload endpoint |
| `captures_max_image_pixels` | 60 000 000 | Decompression-bomb limit (C) |
| `captures_thumbnail_max_px` | 512 | Longer side of the thumbnail |
| `captures_thumbnail_quality` | 80 | JPEG quality, 1 to 95 |
| `captures_gps_boundary_tolerance_m` | 30 | Consumer GPS error (2 to 5 m) plus a drone photographing the edge while flying just outside |
| `captures_default_clock_timezone` | America/Santiago | Zone assumed for EXIF time without offset when the device declares none |
| `captures_max_future_skew_s` | 300 | Beyond this, a capture time in the future is flagged `CLOCK_SKEW` |
| `captures_blur_threshold` | 100.0 | **Provisional, not calibrated** against photos of the target drone |
| `capture_session_auto_close_hours` | 24 | A forgotten open session must not block its device forever |

The application refuses to start when a request may carry more files than a
session accepts, or when the default clock zone is not a valid IANA zone.

Time-zone names are validated in one place, `app/shared/timezones.py`, used
by the configuration now and by device registration later: only IANA
"Region/City" names and `UTC` are valid. Names that load from the system
database but are not zones (`localtime`, `posixrules`), the `posix/` and
`right/` variants, and `Etc/GMT+3` style names (whose sign is the opposite of
what they seem) are refused.

## Constraints found for later blocks

- **The upload route must not go through `BodySizeLimitMiddleware`.** That
  middleware guards the GeoJSON endpoints: it covers the whole
  `/api/v1/predios` prefix with a 2 MB limit (`geo_max_geojson_size_kb`) and
  buffers the full body. The upload endpoint lives under that prefix, so as
  it stands every upload would answer 413, and the body would be held in
  memory, which decision C and the streaming requirement forbid. Block 5
  excludes the upload route from it and enforces its own limits while
  streaming, under three conditions:
  - The exclusion is for the **exact upload route**
    (`/api/v1/predios/{predio_id}/capture-sessions/{session_id}/captures`,
    POST), matched as a whole, **never a prefix**. A prefix such as
    `/api/v1/predios/` plus anything containing `capture-sessions` would let
    any future endpoint under it skip the GeoJSON limit by accident.
  - A test confirms that the GeoJSON endpoints under `/api/v1/predios` still
    answer **413 with a 3 MB body**, so the exclusion cannot widen silently.
  - The upload endpoint refuses early by `Content-Length` (a request
    declaring more than `captures_max_files_per_request` ×
    `captures_max_file_size_mb` plus multipart overhead is rejected before
    reading it), **and still enforces the limits while reading**: the
    declared length is only a fast path, never trusted, since the client can
    lie about it or omit it.
- `max_upload_size_mb` (50) and `storage_path` predated this phase and were
  used nowhere; they were removed so they cannot be mistaken for the capture
  limits. The settings model ignores unknown environment variables
  (`extra="ignore"`), so an old `.env` that still sets them keeps working.

## Consequences

To be completed as the blocks are implemented (adapter registry, validation
pipeline, worker processing, endpoints, audit).

## Related limitations

Known limitations of this phase start at KL-009 and are written as the
blocks that introduce them are implemented.
