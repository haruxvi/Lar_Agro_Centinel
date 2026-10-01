"""Client for Sentinel Hub on Copernicus Data Space, written on httpx.

Not sentinelhub-py: what this phase must control (the token cached in Redis
with a single refresh, Processing Units read from every response, backoff,
``Retry-After``, a circuit breaker) is exactly what that library hides, and it
is synchronous. See docs/dependencies.md and ADR-004.

Scene selection is two steps plus one download:

1. **Catalog** (free) lists the acquisitions in the range and drops tiles that
   are completely cloudy. A tile is ~110 x 110 km, so its cloud cover says
   little about one predio; this is only a cheap pre-filter.
2. **Statistical API**, one call for the whole range at low resolution,
   measures the cloudy fraction over the predio's exact geometry per date.
   Cached in Redis: past cloud cover does not change.
3. **Process API** downloads B04, B08 and SCL at full resolution, only for the
   chosen date.

API details verified against the current documentation (2026-10):
- PU spent per request: response header ``x-processingunits-spent``.
- ``Retry-After`` on 429 is in **milliseconds**, not seconds, unlike HTTP's
  usual meaning ("next request will be available in 3398 ms").
- Harmonisation: ``input.data.processing.harmonizeValues`` (default true).

The client never logs the token or the client secret, and never puts them in
an exception message.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import secrets
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Final, Protocol

import httpx
from redis import Redis as SyncRedis
from redis.asyncio import Redis
from redis.exceptions import LockError
from shapely.geometry.base import BaseGeometry

from app.modules.analysis.exceptions import AnalysisError
from app.modules.audit.events import SENTINEL_CIRCUIT_OPEN, SENTINEL_QUOTA_EXCEEDED
from app.shared.config import Settings
from app.shared.logging import get_logger

logger = get_logger(__name__)

COLLECTION: Final = "sentinel-2-l2a"
CATALOG_PATH: Final = "/catalog/v1/search"
STATISTICS_PATH: Final = "/api/v1/statistics"
PROCESS_PATH: Final = "/api/v1/process"
PU_HEADER: Final = "x-processingunits-spent"
CRS84: Final = "http://www.opengis.net/def/crs/OGC/1.3/CRS84"

# The only bands the client will put in an evalscript. Building the script
# from an allowlist means no caller-supplied string ever reaches it.
BAND_UNITS: Final = {"B04": "REFLECTANCE", "B08": "REFLECTANCE", "SCL": "DN"}

# SCL classes that count as cloud for scene selection: cloud shadow, medium
# and high probability cloud, thin cirrus. NO_DATA (0) is not cloud: it is
# excluded from the denominator through dataMask.
CLOUD_SCL_CLASSES: Final = (3, 8, 9, 10)

# Process API output limit per side, in pixels.
MAX_OUTPUT_PIXELS: Final = 2500
# Margin around the predio's bbox, in output pixels, so the exact clip later
# never loses a border pixel.
BBOX_BUFFER_PIXELS: Final = 2

METRES_PER_DEGREE_LAT: Final = 110_574.0
METRES_PER_DEGREE_LON_AT_EQUATOR: Final = 111_320.0

TOKEN_SAFETY_MARGIN_S: Final = 60
TOKEN_LOCK_TIMEOUT_S: Final = 30
TOKEN_WAIT_S: Final = 15
BACKOFF_BASE_S: Final = 1.0
BACKOFF_MAX_S: Final = 30.0
RETRY_AFTER_MAX_S: Final = 120.0

# Keep monthly counters a little past the month they count.
PU_KEY_TTL_S: Final = 40 * 24 * 3600


# --- errors ------------------------------------------------------------------


class SentinelError(AnalysisError):
    """Base class for Sentinel Hub errors."""


class SentinelAuthError(SentinelError):
    """The OAuth token could not be obtained. Says why, never with what."""


class SentinelRequestError(SentinelError):
    """Sentinel Hub refused a request, or kept failing after the retries."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        """Record the reason and, when there was one, the HTTP status."""
        super().__init__(message)
        self.status_code = status_code

    def as_dict(self) -> dict[str, object]:
        """Return the reason and the status code."""
        return {
            "error": type(self).__name__,
            "message": str(self),
            "status_code": self.status_code,
        }


class SentinelQuotaExceededError(SentinelError):
    """The monthly Processing Unit budget is spent."""

    def __init__(self, spent: float, budget: int) -> None:
        """Record how much was spent against which budget."""
        super().__init__(f"{spent:.2f} of {budget} PU spent this month")
        self.spent = spent
        self.budget = budget

    def as_dict(self) -> dict[str, object]:
        """Return the spending against the budget."""
        return {
            "error": type(self).__name__,
            "spent_pu": round(self.spent, 2),
            "budget_pu": self.budget,
        }


class SentinelCircuitOpenError(SentinelError):
    """Recent requests kept failing; the client is not trying for a while."""

    def __init__(self, retry_in_s: int) -> None:
        """Record when the next attempt will be allowed."""
        super().__init__(f"circuit open, retry in {retry_in_s} s")
        self.retry_in_s = retry_in_s

    def as_dict(self) -> dict[str, object]:
        """Return when to try again."""
        return {"error": type(self).__name__, "retry_in_s": self.retry_in_s}


@dataclass(frozen=True)
class CloudObservation:
    """One acquisition date, as measured over the predio."""

    acquired_on: date
    cloud_fraction: float | None
    """Cloudy share of the valid pixels; None when no pixel was valid."""
    valid_pixels: int

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe form, for analyses.error_detail."""
        return {
            "date": self.acquired_on.isoformat(),
            "cloud_fraction": (
                None if self.cloud_fraction is None else round(self.cloud_fraction, 4)
            ),
            "valid_pixels": self.valid_pixels,
        }


class NoSuitableSceneFoundError(SentinelError):
    """No acquisition in the range was clear enough over the predio.

    Not a system failure: the sky was cloudy. Carries every date evaluated
    and why it was discarded, so the user can be told more than "no data".
    """

    def __init__(
        self,
        evaluated: Sequence[CloudObservation],
        max_cloud_coverage: float,
        tile_level_rejections: Sequence[dict[str, object]] = (),
    ) -> None:
        """Record what was evaluated and against which threshold."""
        super().__init__(f"no scene under {max_cloud_coverage:.0%} cloud cover")
        self.evaluated = list(evaluated)
        self.max_cloud_coverage = max_cloud_coverage
        self.tile_level_rejections = list(tile_level_rejections)

    def as_dict(self) -> dict[str, object]:
        """Return the evaluation, ready for analyses.error_detail."""
        measured = [
            o.cloud_fraction for o in self.evaluated if o.cloud_fraction is not None
        ]
        return {
            "error": type(self).__name__,
            "max_cloud_coverage": self.max_cloud_coverage,
            "scenes_evaluated": len(self.evaluated),
            "best_cloud_fraction": round(min(measured), 4) if measured else None,
            "evaluated": [o.as_dict() for o in self.evaluated],
            "tile_level_rejections": self.tile_level_rejections,
        }


# --- results -----------------------------------------------------------------


@dataclass(frozen=True)
class SceneCandidate:
    """An acquisition listed by the Catalog."""

    scene_id: str
    acquired_at: datetime
    tile_cloud_cover: float | None
    """Cloud cover of the whole ~110 km tile, in percent. Pre-filter only."""


@dataclass(frozen=True)
class SceneSelection:
    """The acquisition chosen for analysis, and everything that was weighed."""

    scene_date: date
    scene_id: str
    cloud_coverage: float
    evaluated: list[CloudObservation]


@dataclass(frozen=True)
class SentinelResponse:
    """A downloaded raster and what it cost."""

    data: bytes
    content_type: str
    bands: tuple[str, ...]
    """Band order in the raster. dataMask is always appended last."""
    bbox: tuple[float, float, float, float]
    crs: str
    resolution: tuple[float, float]
    """(x, y) pixel size in degrees."""
    processing_units: float


@dataclass(frozen=True)
class PuUsage:
    """Processing Units spent this month against the budget."""

    month: str
    spent: float
    budget: int

    @property
    def remaining(self) -> float:
        """Return what is left of the budget, never negative."""
        return max(0.0, self.budget - self.spent)


def pu_month_key(prefix: str = "sh", now: datetime | None = None) -> str:
    """Return the Redis key holding a month's Processing Unit spending."""
    moment = now or datetime.now(UTC)
    return f"{prefix}:pu:{moment:%Y-%m}"


def _usage_from(raw: object, settings: Settings) -> PuUsage:
    if isinstance(raw, bytes):
        raw = raw.decode()
    return PuUsage(
        month=f"{datetime.now(UTC):%Y-%m}",
        spent=float(str(raw)) if raw else 0.0,
        budget=settings.sentinel_monthly_pu_budget,
    )


def read_pu_usage(
    redis: SyncRedis, settings: Settings, *, key_prefix: str = "sh"
) -> PuUsage:
    """Return this month's spending from a synchronous Redis client.

    For the API process, which checks the budget before queueing without
    building a client (and an HTTP connection) to Sentinel Hub.
    """
    return _usage_from(redis.get(pu_month_key(key_prefix)), settings)


class SentinelEventSink(Protocol):
    """Where the client reports events that belong in the audit trail."""

    def __call__(self, event_type: str, details: dict[str, object]) -> Awaitable[None]:
        """Record ``event_type`` with ``details`` (no credentials, no rasters)."""
        ...


Sleep = Callable[[float], Awaitable[None]]


@dataclass
class _Attempt:
    """Book-keeping for one logical request across its retries."""

    retries_left: int
    backoff_s: float = BACKOFF_BASE_S
    errors: list[str] = field(default_factory=list)


# --- geometry helpers --------------------------------------------------------


def degrees_for_metres(metres: float, latitude: float) -> tuple[float, float]:
    """Return (x, y) degrees spanning ``metres`` at ``latitude``.

    Rasters are requested in EPSG:4326, like every stored geometry, so no UTM
    zone has to be chosen (see ADR-003). Pixels are ~``metres`` wide on the
    ground at the predio's latitude.
    """
    lon = metres / (METRES_PER_DEGREE_LON_AT_EQUATOR * math.cos(math.radians(latitude)))
    lat = metres / METRES_PER_DEGREE_LAT
    return lon, lat


def buffered_bbox(
    geometry: BaseGeometry, resolution: tuple[float, float]
) -> tuple[float, float, float, float]:
    """Return the geometry's bbox grown by a few pixels on every side."""
    min_x, min_y, max_x, max_y = geometry.bounds
    dx, dy = resolution[0] * BBOX_BUFFER_PIXELS, resolution[1] * BBOX_BUFFER_PIXELS
    return (min_x - dx, min_y - dy, max_x + dx, max_y + dy)


def cloud_cache_key(
    prefix: str, geometry: BaseGeometry, date_from: date, date_to: date
) -> str:
    """Key of a cached cloud measurement: sh:cloud:{sha256(wkb)}:{from}:{to}."""
    digest = hashlib.sha256(geometry.wkb).hexdigest()
    return f"{prefix}:cloud:{digest}:{date_from.isoformat()}:{date_to.isoformat()}"


def _iso_day_start(day: date) -> str:
    return f"{day.isoformat()}T00:00:00Z"


def _iso_day_end(day: date) -> str:
    return f"{day.isoformat()}T23:59:59Z"


# --- evalscripts -------------------------------------------------------------


def cloud_evalscript() -> str:
    """Per-pixel cloud indicator from SCL, for the Statistical API.

    The mean of ``cloud`` over valid pixels is the cloudy fraction. Pixels
    without data (dataMask 0 or SCL 0) are excluded through dataMask, so they
    neither count as clear nor as cloudy.
    """
    cloudy = " || ".join(f"s.SCL == {value}" for value in CLOUD_SCL_CLASSES)
    return (
        "//VERSION=3\n"
        "function setup() {\n"
        "  return {\n"
        '    input: [{ bands: ["SCL", "dataMask"] }],\n'
        "    output: [\n"
        '      { id: "cloud", bands: 1, sampleType: "UINT8" },\n'
        '      { id: "dataMask", bands: 1 }\n'
        "    ]\n"
        "  };\n"
        "}\n"
        "function evaluatePixel(s) {\n"
        "  var valid = s.dataMask == 1 && s.SCL != 0;\n"
        f"  var cloudy = ({cloudy}) ? 1 : 0;\n"
        "  return { cloud: [cloudy], dataMask: [valid ? 1 : 0] };\n"
        "}\n"
    )


def bands_evalscript(bands: Sequence[str]) -> str:
    """Return the evalscript for the given bands plus dataMask, as FLOAT32.

    Reflectance bands are requested in REFLECTANCE units: Sentinel Hub applies
    the scale factor and, with harmonizeValues, removes the processing
    baseline 04.00 offset. SCL is a class band and comes as DN.
    """
    unknown = [band for band in bands if band not in BAND_UNITS]
    if unknown or not bands or len(set(bands)) != len(bands):
        raise SentinelRequestError(f"unsupported band list: {list(bands)}")
    band_list = json.dumps([*bands, "dataMask"])
    units = json.dumps([*(BAND_UNITS[band] for band in bands), "DN"])
    samples = ", ".join(f"s.{band}" for band in [*bands, "dataMask"])
    return (
        "//VERSION=3\n"
        "function setup() {\n"
        "  return {\n"
        f"    input: [{{ bands: {band_list}, units: {units} }}],\n"
        f'    output: {{ bands: {len(bands) + 1}, sampleType: "FLOAT32" }}\n'
        "  };\n"
        "}\n"
        "function evaluatePixel(s) {\n"
        f"  return [{samples}];\n"
        "}\n"
    )


# --- the client --------------------------------------------------------------


class SentinelClient:
    """Talks to Sentinel Hub with budgeting, retries and a circuit breaker.

    State shared between worker processes (token, PU counter, circuit) lives
    in Redis, so every worker sees the same budget and the same outage.
    """

    def __init__(
        self,
        settings: Settings,
        redis: Redis,
        events: SentinelEventSink,
        *,
        http: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        key_prefix: str = "sh",
    ) -> None:
        """Bind the client to its configuration, Redis and an event sink."""
        if not settings.sentinel_configured:
            # Callers check availability first; this is the last guard.
            raise SentinelAuthError("Sentinel Hub credentials are not configured")
        self._settings = settings
        self._redis = redis
        self._events = events
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.sentinel_request_timeout_s)
        )
        self._sleep = sleep
        self._prefix = key_prefix
        self._random = secrets.SystemRandom()
        self._spent = 0.0

    # --- keys ---------------------------------------------------------------------

    @property
    def _token_key(self) -> str:
        return f"{self._prefix}:token"

    @property
    def _token_lock_key(self) -> str:
        return f"{self._prefix}:token:lock"

    def _pu_key(self, now: datetime | None = None) -> str:
        return pu_month_key(self._prefix, now)

    @property
    def _circuit_open_key(self) -> str:
        return f"{self._prefix}:circuit:open"

    @property
    def _circuit_failures_key(self) -> str:
        return f"{self._prefix}:circuit:failures"

    # --- token --------------------------------------------------------------------

    async def get_token(self) -> str:
        """Return a valid access token, refreshing it at most once at a time.

        Many workers asking at once: one takes the Redis lock and refreshes,
        the others wait for the cached token instead of each fetching one.
        """
        cached = await self._redis.get(self._token_key)
        if cached:
            return _text(cached)

        lock = self._redis.lock(
            self._token_lock_key,
            timeout=TOKEN_LOCK_TIMEOUT_S,
            blocking_timeout=TOKEN_WAIT_S,
        )
        acquired = await lock.acquire()
        if not acquired:
            raise SentinelAuthError("timed out waiting for another token refresh")
        try:
            # Whoever held the lock before us may have refreshed already.
            cached = await self._redis.get(self._token_key)
            if cached:
                return _text(cached)
            return await self._refresh_token()
        finally:
            try:
                await lock.release()
            except LockError:
                # The lock expired during a slow refresh; nothing left to free.
                logger.warning("sentinel_token_lock_expired")

    async def _refresh_token(self) -> str:
        client_id = self._settings.sentinel_client_id
        client_secret = self._settings.sentinel_client_secret
        if client_id is None or client_secret is None:
            raise SentinelAuthError("Sentinel Hub credentials are not configured")
        try:
            response = await self._http.post(
                self._settings.sentinel_token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id.get_secret_value(),
                    "client_secret": client_secret.get_secret_value(),
                },
            )
        except httpx.HTTPError as exc:
            raise SentinelAuthError(
                f"token request failed: {type(exc).__name__}"
            ) from None
        if response.status_code != httpx.codes.OK:
            # The body may echo request details; it is not logged or raised.
            raise SentinelAuthError(f"token endpoint answered {response.status_code}")
        try:
            payload = response.json()
            token = payload.get("access_token")
            expires_in = int(payload.get("expires_in", 0))
        except (ValueError, AttributeError, TypeError):
            raise SentinelAuthError(
                "token endpoint returned an unreadable body"
            ) from None
        if not isinstance(token, str) or not token:
            raise SentinelAuthError("token endpoint returned no access token")
        ttl = max(1, expires_in - TOKEN_SAFETY_MARGIN_S)
        await self._redis.set(self._token_key, token, ex=ttl)
        logger.info("sentinel_token_refreshed", ttl_s=ttl)
        return token

    async def _forget_token(self) -> None:
        await self._redis.delete(self._token_key)

    # --- processing units ----------------------------------------------------------

    @property
    def processing_units_spent(self) -> float:
        """Return the PU spent through this client instance.

        One instance per analysis job, so this is that analysis's cost,
        unmixed with jobs running concurrently on other instances.
        """
        return self._spent

    async def pu_usage(self) -> PuUsage:
        """Return this month's spending against the budget (for health checks)."""
        raw = await self._redis.get(self._pu_key())
        return _usage_from(raw, self._settings)

    async def ensure_budget(self) -> None:
        """Raise, and record the event, when the month's budget is spent."""
        usage = await self.pu_usage()
        if usage.spent >= usage.budget:
            await self._events(
                SENTINEL_QUOTA_EXCEEDED,
                {"spent_pu": round(usage.spent, 2), "budget_pu": usage.budget},
            )
            raise SentinelQuotaExceededError(usage.spent, usage.budget)

    async def _account(self, response: httpx.Response) -> float:
        raw = response.headers.get(PU_HEADER)
        if raw is None:
            return 0.0
        try:
            spent = float(raw)
        except ValueError:
            logger.warning("sentinel_pu_header_unreadable")
            return 0.0
        if spent > 0:
            self._spent += spent
            key = self._pu_key()
            await self._redis.incrbyfloat(key, spent)
            await self._redis.expire(key, PU_KEY_TTL_S)
        return spent

    # --- circuit breaker ------------------------------------------------------------

    async def _ensure_circuit_closed(self) -> None:
        ttl = await self._redis.ttl(self._circuit_open_key)
        if ttl and ttl > 0:
            raise SentinelCircuitOpenError(int(ttl))

    async def _record_failure(self, reason: str) -> None:
        failures = await self._redis.incr(self._circuit_failures_key)
        await self._redis.expire(
            self._circuit_failures_key, self._settings.sentinel_circuit_reset_s * 4
        )
        if failures >= self._settings.sentinel_circuit_failure_threshold:
            opened = await self._redis.set(
                self._circuit_open_key,
                "1",
                ex=self._settings.sentinel_circuit_reset_s,
                nx=True,
            )
            if opened:
                await self._redis.delete(self._circuit_failures_key)
                logger.warning("sentinel_circuit_opened", failures=failures)
                await self._events(
                    SENTINEL_CIRCUIT_OPEN,
                    {
                        "consecutive_failures": failures,
                        "reset_s": self._settings.sentinel_circuit_reset_s,
                        "last_error": reason,
                    },
                )

    async def _record_success(self) -> None:
        await self._redis.delete(self._circuit_failures_key)

    # --- transport ------------------------------------------------------------------

    async def _send(
        self, method: str, path: str, *, billable: bool, **kwargs: Any
    ) -> httpx.Response:
        """Send one logical request, with budget, retries and the breaker.

        Retries 5xx, timeouts and transport errors with exponential backoff,
        and 429 after its Retry-After (milliseconds). Never retries other 4xx.
        A request that still fails after its retries counts once against the
        circuit breaker; client errors and rate limits do not count at all.
        """
        await self._ensure_circuit_closed()
        if billable:
            await self.ensure_budget()

        url = f"{self._settings.sentinel_base_url}{path}"
        extra_headers: dict[str, str] = kwargs.pop("headers", {})
        attempt = _Attempt(retries_left=self._settings.sentinel_max_retries)
        refreshed_token = False
        while True:
            token = await self.get_token()
            headers = {"Authorization": f"Bearer {token}", **extra_headers}
            try:
                response = await self._http.request(
                    method,
                    url,
                    headers=headers,
                    timeout=self._settings.sentinel_request_timeout_s,
                    **kwargs,
                )
            except httpx.TimeoutException:
                failure = "timeout"
            except httpx.TransportError as exc:
                failure = f"transport error ({type(exc).__name__})"
            else:
                status = response.status_code
                if status < 400:
                    await self._record_success()
                    await self._account(response)
                    return response
                if status == httpx.codes.UNAUTHORIZED and not refreshed_token:
                    # The cached token was revoked or expired early: once.
                    refreshed_token = True
                    await self._forget_token()
                    continue
                if status == httpx.codes.TOO_MANY_REQUESTS:
                    if attempt.retries_left <= 0:
                        raise SentinelRequestError("rate limited", status)
                    attempt.retries_left -= 1
                    await self._sleep(self._retry_after_s(response, attempt))
                    continue
                if status < 500:
                    raise SentinelRequestError(f"request refused ({status})", status)
                failure = f"server error ({status})"

            attempt.errors.append(failure)
            if attempt.retries_left <= 0:
                await self._record_failure(failure)
                raise SentinelRequestError(
                    f"gave up after {len(attempt.errors)} attempts: {failure}"
                )
            attempt.retries_left -= 1
            await self._sleep(self._next_backoff(attempt))

    def _next_backoff(self, attempt: _Attempt) -> float:
        delay = min(attempt.backoff_s, BACKOFF_MAX_S)
        attempt.backoff_s *= 2
        # Jitter keeps concurrent workers from retrying in lockstep.
        return delay + self._random.uniform(0, delay / 4)

    def _retry_after_s(self, response: httpx.Response, attempt: _Attempt) -> float:
        raw = response.headers.get("retry-after")
        try:
            # Sentinel Hub sends milliseconds, not the usual HTTP seconds.
            return min(float(raw) / 1000.0, RETRY_AFTER_MAX_S) if raw else 0.0
        except ValueError:
            return self._next_backoff(attempt)

    # --- catalog ----------------------------------------------------------------------

    async def search_scenes(
        self, geometry: BaseGeometry, date_from: date, date_to: date
    ) -> list[SceneCandidate]:
        """List every Sentinel-2 L2A acquisition over the predio's bbox."""
        body: dict[str, Any] = {
            "bbox": list(geometry.bounds),
            "datetime": f"{_iso_day_start(date_from)}/{_iso_day_end(date_to)}",
            "collections": [COLLECTION],
            "limit": 100,
            "fields": {
                "include": ["id", "properties.datetime", "properties.eo:cloud_cover"]
            },
        }
        scenes: list[SceneCandidate] = []
        while True:
            response = await self._send("POST", CATALOG_PATH, billable=False, json=body)
            payload = response.json()
            for feature in payload.get("features", []):
                properties = feature.get("properties", {})
                cloud = properties.get("eo:cloud_cover")
                scenes.append(
                    SceneCandidate(
                        scene_id=str(feature["id"]),
                        acquired_at=datetime.fromisoformat(
                            str(properties["datetime"]).replace("Z", "+00:00")
                        ),
                        tile_cloud_cover=None if cloud is None else float(cloud),
                    )
                )
            next_token = payload.get("context", {}).get("next")
            if next_token is None:
                return scenes
            body["next"] = next_token

    # --- statistical ------------------------------------------------------------------

    async def cloud_coverage_by_date(
        self, geometry: BaseGeometry, date_from: date, date_to: date
    ) -> list[CloudObservation]:
        """Return the cloudy fraction over the predio for each acquisition date.

        One Statistical API call for the whole range, at
        sentinel_cloud_stats_resolution_m, cached for
        sentinel_cloud_stats_cache_ttl_s.
        """
        key = cloud_cache_key(self._prefix, geometry, date_from, date_to)
        cached = await self._redis.get(key)
        if cached:
            return [_observation_from_dict(item) for item in json.loads(_text(cached))]

        latitude = geometry.centroid.y
        resx, resy = degrees_for_metres(
            self._settings.sentinel_cloud_stats_resolution_m, latitude
        )
        body = {
            "input": {
                "bounds": {
                    "geometry": json.loads(json.dumps(geometry.__geo_interface__)),
                    "properties": {"crs": CRS84},
                },
                "data": [{"type": COLLECTION}],
            },
            "aggregation": {
                "timeRange": {
                    "from": _iso_day_start(date_from),
                    "to": _iso_day_end(date_to),
                },
                "aggregationInterval": {"of": "P1D"},
                "evalscript": cloud_evalscript(),
                "resx": resx,
                "resy": resy,
            },
        }
        response = await self._send("POST", STATISTICS_PATH, billable=True, json=body)
        observations = parse_cloud_statistics(response.json())
        await self._redis.set(
            key,
            json.dumps([o.as_dict() for o in observations]),
            ex=self._settings.sentinel_cloud_stats_cache_ttl_s,
        )
        return observations

    # --- selection --------------------------------------------------------------------

    async def select_scene(
        self, geometry: BaseGeometry, date_from: date, date_to: date
    ) -> SceneSelection:
        """Pick the most recent acquisition clear enough over the predio."""
        threshold = self._settings.analysis_max_cloud_coverage
        candidates = await self.search_scenes(geometry, date_from, date_to)

        usable = [c for c in candidates if not _fully_cloudy(c)]
        tile_rejections: list[dict[str, object]] = [
            {
                "scene_id": c.scene_id,
                "date": c.acquired_at.date().isoformat(),
                "tile_cloud_cover": c.tile_cloud_cover,
            }
            for c in candidates
            if _fully_cloudy(c)
        ]
        if not usable:
            # Nothing worth measuring: no PU spent on the Statistical API.
            raise NoSuitableSceneFoundError([], threshold, tile_rejections)

        # Measure only the span that has candidates: same answer, fewer days.
        first = min(c.acquired_at.date() for c in usable)
        last = max(c.acquired_at.date() for c in usable)
        observations = await self.cloud_coverage_by_date(geometry, first, last)

        candidate_dates = {c.acquired_at.date() for c in usable}
        evaluated = sorted(
            (o for o in observations if o.acquired_on in candidate_dates),
            key=lambda o: o.acquired_on,
            reverse=True,
        )
        for observation in evaluated:
            fraction = observation.cloud_fraction
            if fraction is not None and fraction <= threshold:
                ids = sorted(
                    c.scene_id
                    for c in usable
                    if c.acquired_at.date() == observation.acquired_on
                )
                return SceneSelection(
                    scene_date=observation.acquired_on,
                    scene_id="+".join(ids),
                    cloud_coverage=fraction,
                    evaluated=evaluated,
                )
        raise NoSuitableSceneFoundError(evaluated, threshold, tile_rejections)

    # --- process ----------------------------------------------------------------------

    async def fetch_bands(
        self,
        geometry: BaseGeometry,
        date_from: date,
        date_to: date,
        bands: list[str],
    ) -> SentinelResponse:
        """Download ``bands`` (plus dataMask) over the predio's buffered bbox.

        Output is a FLOAT32 GeoTIFF in EPSG:4326 at ~analysis_resolution_m.
        SCL is upsampled with NEAREST, declared explicitly: interpolating a
        class mask would invent classes that do not exist.
        """
        latitude = geometry.centroid.y
        resolution = degrees_for_metres(self._settings.analysis_resolution_m, latitude)
        bbox = buffered_bbox(geometry, resolution)
        width = math.ceil((bbox[2] - bbox[0]) / resolution[0])
        height = math.ceil((bbox[3] - bbox[1]) / resolution[1])
        if max(width, height) > MAX_OUTPUT_PIXELS:
            raise SentinelRequestError(
                f"predio too large for one request ({width}x{height} px)"
            )

        body = {
            "input": {
                "bounds": {"bbox": list(bbox), "properties": {"crs": CRS84}},
                "data": [
                    {
                        "type": COLLECTION,
                        "dataFilter": {
                            "timeRange": {
                                "from": _iso_day_start(date_from),
                                "to": _iso_day_end(date_to),
                            },
                            "mosaickingOrder": "mostRecent",
                        },
                        "processing": {
                            # Removes the baseline 04.00 offset server-side.
                            "harmonizeValues": True,
                            "upsampling": "NEAREST",
                            "downsampling": "NEAREST",
                        },
                    }
                ],
            },
            "output": {
                "resx": resolution[0],
                "resy": resolution[1],
                "responses": [
                    {"identifier": "default", "format": {"type": "image/tiff"}}
                ],
            },
            "evalscript": bands_evalscript(bands),
        }
        response = await self._send(
            "POST",
            PROCESS_PATH,
            billable=True,
            json=body,
            headers={"Accept": "image/tiff"},
        )
        return SentinelResponse(
            data=response.content,
            content_type=response.headers.get("content-type", "image/tiff"),
            bands=(*bands, "dataMask"),
            bbox=bbox,
            crs="EPSG:4326",
            resolution=resolution,
            processing_units=float(response.headers.get(PU_HEADER, 0) or 0),
        )

    async def aclose(self) -> None:
        """Close the HTTP client."""
        await self._http.aclose()


# --- parsing -----------------------------------------------------------------


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


def _fully_cloudy(candidate: SceneCandidate) -> bool:
    return candidate.tile_cloud_cover is not None and candidate.tile_cloud_cover >= 100.0


def _observation_from_dict(item: dict[str, Any]) -> CloudObservation:
    fraction = item.get("cloud_fraction")
    return CloudObservation(
        acquired_on=date.fromisoformat(str(item["date"])),
        cloud_fraction=None if fraction is None else float(fraction),
        valid_pixels=int(item.get("valid_pixels", 0)),
    )


def parse_cloud_statistics(payload: dict[str, Any]) -> list[CloudObservation]:
    """Turn a Statistical API response into one observation per date.

    Intervals that failed carry an "error" instead of "outputs" and are
    skipped. An interval with no valid pixel has no meaningful mean: its
    cloud fraction is None, never 0 (which would read as "perfectly clear").
    """
    observations: list[CloudObservation] = []
    for item in payload.get("data", []):
        if "outputs" not in item:
            continue
        day = date.fromisoformat(str(item["interval"]["from"])[:10])
        stats = item["outputs"]["cloud"]["bands"]["B0"]["stats"]
        valid = int(stats.get("sampleCount", 0)) - int(stats.get("noDataCount", 0))
        mean = stats.get("mean")
        fraction: float | None
        try:
            fraction = float(mean) if valid > 0 else None
        except (TypeError, ValueError):
            fraction = None
        if fraction is not None and not math.isfinite(fraction):
            fraction = None
        observations.append(CloudObservation(day, fraction, max(valid, 0)))
    return observations
