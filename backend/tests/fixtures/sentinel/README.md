# Sentinel Hub fixtures

Everything here is **synthetic**. No file was downloaded from Sentinel Hub,
and no test ever calls the real API (see the guard in `backend/tests/`).

The JSON files follow the structure of the official examples of each API,
verified against the documentation in 2026-10:

| File | Shape taken from | What it contains |
|---|---|---|
| `token_response.json` | OAuth2 client-credentials response | A fake token, `expires_in` 600 s |
| `catalog_search.json` | Catalog API (STAC `FeatureCollection`) | Five tiles over four dates: one date with two tiles (`T19HCC` and `T19HCD`), and one tile 100% cloudy that the pre-filter drops |
| `statistics_cloud.json` | Statistical API, `aggregationInterval: P1D` | Cloudy fraction per date over the predio: 0.00 (09-10), 0.12 (09-15), 0.85 (09-20) |

With the default threshold (30%), the scene selection must pick 2026-09-15:
the most recent date under the threshold, even though 09-20 is newer and its
tile-level cloud cover (12.4%) looked fine. That is the case the two-step
design exists for.
