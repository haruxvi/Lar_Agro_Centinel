# ADR-003: geospatial model for predios and lotes

- **Status:** Accepted
- **Date:** 2026-09-24
- **Deciders:** haruxvi

## Context

Every other piece of data in the system hangs from a predio and its lotes:
captures, analyses, applications, stock movements. Their boundaries are
geometry, and their **areas are not cosmetic**: they feed doses per hectare,
coverage figures and per-surface analysis, all of which carry regulatory
weight (SAG traceability).

Boundaries arrive from untrusted sources: drawn by hand on a map, exported from
Google Earth or a GPS, imported as GeoJSON. They are frequently invalid in
small ways, sometimes in large ones, and occasionally hostile (a payload sized
to exhaust memory).

The system is multi-tenant: one owner must never learn, through a geometry
operation, that another owner's predio exists.

## Decision

### Storage: SRID 4326, metrics on `geography`

Geometries are stored in **WGS84 (SRID 4326)**, the reference system of GeoJSON,
drone GPS and satellite products, so nothing is reprojected on the way in or
out. Every metric computation (area, distance, containment buffer) casts to
`geography` in PostGIS and is measured on the ellipsoid.

There is **no fixed UTM projection**. Continental Chile spans UTM zones 18S and
19S, and Rapa Nui sits in 12S; any single zone would be wrong somewhere, and
per-predio zone selection is a decision that can be gotten wrong silently.
`geography` has no zone to choose. Areas agree with an independent geodesic
computation (pyproj) within 0.1%, which a test enforces.

### Columns and constraints

- `geometry` is a generic `GEOMETRY(4326)` plus a CHECK that it is a Polygon or
  MultiPolygon: PostGIS type modifiers cannot say "one of two types", while the
  typmod still pins the SRID.
- `area_m2` and `centroid` are **derived in the database**, never taken from
  the client, and stored so listings can return them without loading or
  shipping geometries. A CHECK keeps `area_m2 > 0`.
- GiST indexes on `predios.geometry`, `predios.centroid` and `lotes.geometry`.
- Predios and lotes are **soft-deleted**; uniqueness (a predio's slug per
  owner, a lote's name per predio) is enforced by partial unique indexes over
  live rows only. Foreign keys from lotes and role grants are `RESTRICT`, so a
  physical delete fails rather than cascades.

### Validation pipeline

Untrusted GeoJSON goes through, in order:

1. **Size**, at the ASGI layer, before a byte is parsed: bodies over
   `geo_max_geojson_size_kb` answer 413, whether or not `Content-Length` is
   honest.
2. **Structure**, on the raw JSON, before Shapely: type (Polygon or
   MultiPolygon only), a vertex budget (`geo_max_polygon_vertices`) checked
   while counting, and coordinate ranges. A pathological polygon is refused
   before it costs memory.
3. **Validity and repair.** An invalid geometry is repaired with
   `make_valid(method="structure")`, which dissolves overlapping parts into
   their union. The older `"linework"` method punched a hole wherever two parts
   overlapped, silently dropping area (25% in our reference case). `structure`
   needs GEOS 3.10+; the application refuses to start on an older GEOS, with no
   fallback, because a repair that behaves differently per machine is worse
   than none. A repair whose result is not polygonal is rejected.
4. **Repair policy.** A repair is stored without asking only when its effect
   on the area is small by *either* measure: under `geo_repair_ignore_below_m2`
   (forgives digitising noise on small parcels) or under
   `geo_repair_max_area_change_ratio` (protects large predios). Otherwise the
   API answers 422 with the repaired geometry as a preview, and the client
   resends with `accept_repair: true` if the person confirms it.
   Two cases are **not measurable** and always ask: a reference area of about
   zero (a symmetric bowtie cancels out), and a ring that crosses itself (the
   system cannot tell the contour from the pieces). The reference "before"
   area assumes overlapping parts meant their union; that assumption and the
   unreliability of measuring an invalid geometry are documented where the
   code relies on them (`app/shared/geo.py`).
5. **Area bounds** for predios (`geo_min_predio_area_m2`,
   `geo_max_predio_area_m2`): sanity limits against capture errors such as
   swapped coordinates, not business rules.

A stored repair is marked on the row (`geometry_was_repaired`,
`geometry_repair_area_delta_m2`), so a dose computed on a repaired area can be
traced years later without reconstructing the history.

### Lotes

- A lote must lie within its predio, with a tolerance of
  `geo_lote_containment_tolerance_m` (5 m) applied as a `geography` buffer,
  to absorb hand-digitising imprecision on shared borders.
- Sibling lotes must not share area. Touching along a border is allowed.
- A lote is only reachable through its own predio: a lote id from another
  predio answers 404, not 403, so its existence is not disclosed.

### Overlaps between predios

Overlapping predios are **allowed and reported as a warning**, because boundary
disputes between neighbours are real. The check only looks at the **same
owner's** predios: reporting an overlap with another tenant's predio would
reveal that it exists and where.

### Audit

Geometry events record summaries (area, bbox, vertex count, type), never the
geometry itself; the service refuses to write audit details that carry one.
See [audit-events.md](../audit-events.md).

### Geometry history

Boundaries are overwritten on edit and their previous versions are not kept.
This is deliberate, known debt, recorded with its reasons and consequences in
**[KL-001](../KNOWN-LIMITATIONS.md#kl-001--las-geometrías-de-predios-y-lotes-no-tienen-versionado-histórico)**;
this ADR does not repeat it. The `PREDIO_GEOMETRY_CHANGED` and
`LOTE_GEOMETRY_CHANGED` events are, until that is resolved, the only trace
that a boundary had another shape.

## Alternatives considered

1. **A projected CRS (UTM) for storage.** Rejected: Chile spans several zones,
   and a wrong zone distorts areas silently.
2. **`geography` as the column type.** Rejected: fewer functions and operators
   are available on it, and GeoJSON, captures and imagery are all 4326
   geometry anyway. Casting where a metric is needed gives the same accuracy.
3. **Accepting the area from the client.** Rejected: the area is a regulatory
   figure, and the client is untrusted.
4. **Never repairing, always rejecting.** Rejected: GPS and GIS exports are
   routinely invalid in harmless ways, and a user with no GIS tool cannot fix
   them. Repair with a preview gives them a way forward.
5. **Repairing always, silently.** Rejected: the repaired shape can differ
   materially from what was drawn, and the difference reaches the area.

## Consequences

- A GEOS older than 3.10 prevents startup. Shapely wheels ship their own GEOS
  (3.13.1 today). The backend image installs wheels only (`--only-binary`) and
  no system GDAL: rasterio, fiona and pyogrio load the GDAL bundled in their
  wheels, verified with `ldd` inside the image, and the full suite passes
  there.
- Every endpoint that writes a geometry can answer 422 asking for
  confirmation, and the frontend must render the preview and resend with
  `accept_repair`.
- Listings never return geometries; a map that needs many boundaries at once
  will need a dedicated endpoint (tiles or simplified geometries), designed
  when that screen exists.
- The PostGIS extension is created by the Phase 2 migration, so a database
  created by the test suite or a fresh volume needs no manual step.
