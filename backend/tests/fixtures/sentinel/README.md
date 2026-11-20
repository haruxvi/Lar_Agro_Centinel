# Sentinel Hub fixtures

Everything here is **synthetic**. No file was downloaded from Sentinel Hub,
and no test ever calls the real API (see the guard in `backend/tests/`).

## The rule for doubles of an external API

A double of an external API has to **implement the documented rules that
affect the result**, not just return something with the right shape.

A canned response answers whatever it is asked. On 2026-11-20, against the
real Copernicus API, scene selection turned out never to choose the newest
acquisition of a range: the request ended at 23:59:59, and the Statistical
API drops an incomplete last interval by default (`lastIntervalBehavior:
SKIP`). Every test passed, because the canned `statistics_cloud.json`
answered for that day anyway, which the real API does not do. See ADR-004.

So:

- When the API's answer depends on the request (ranges, intervals, paging,
  units, defaults), the double reads the request and applies the rule:
  `statistical_api.py` (`StatisticalApiDouble`) is the example.
- A static JSON file is only for testing a **parser** on a sample, and it must
  be the real answer to one exact request, stated below.
- When a test against the real API reveals a rule the double did not model,
  fix the double first, then add the test that fails without the fix.

## Files

| File | What it is | Contents |
|---|---|---|
| `statistical_api.py` | Double of the Statistical API (`StatisticalApiDouble`) | Splits `timeRange` into P1D intervals from `from`; an incomplete last interval follows `lastIntervalBehavior` (SKIP by default, SHORTEN, EXTEND); answers only the days with an acquisition. `SEPTEMBER_CLOUD`: cloudy fraction over the predio 0.00 (09-10), 0.12 (09-15), 0.85 (09-20). Every test that runs a request through the client uses it. |
| `statistics_cloud.json` | Parser sample only | The answer to `timeRange` 2026-09-10T00:00:00Z → 2026-09-20T23:59:59Z, P1D, `lastIntervalBehavior: SHORTEN`: three intervals, the last one cut at 23:59:59. Used only by the parser test. |
| `token_response.json` | OAuth2 client-credentials response | A fake token, `expires_in` 600 s |
| `catalog_search.json` | Catalog API (STAC `FeatureCollection`) | Five tiles over four dates: one date with two tiles (`T19HCC` and `T19HCD`), and one tile 100% cloudy that the pre-filter drops. Static: valid only for the September range the tests request (the Catalog filters by `datetime`; the file contains nothing outside it). |
| `fake_client.py` | Double of the client as the pipeline sees it (`SentinelGateway`) | Synthetic GeoTIFF scenes on the real grid; not an API double |

With the default threshold (30%), the scene selection must pick 2026-09-15:
the most recent date under the threshold, even though 09-20 is newer and its
tile-level cloud cover (12.4%) looked fine. That is the case the two-step
design exists for.

The JSON files follow the structure of the official examples of each API,
verified against the documentation in 2026-10 and, for the Statistical API,
against the real service on 2026-11-20.
