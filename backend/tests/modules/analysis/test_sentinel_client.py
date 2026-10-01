"""Sentinel Hub client: token, budget, scene selection, retries, breaker.

No test reaches the network. Every request goes through httpx.MockTransport,
and the configured URLs point to ``.invalid`` hosts as a second line: even a
mistake in the mock wiring could not reach the real API. Shared state lives
in the real test Redis, under a per-test key prefix.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.exceptions import RedisError
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from app.modules.analysis.sentinel_client import (
    CATALOG_PATH,
    PROCESS_PATH,
    PU_HEADER,
    SENTINEL_CIRCUIT_OPEN,
    SENTINEL_QUOTA_EXCEEDED,
    STATISTICS_PATH,
    NoSuitableSceneFoundError,
    SentinelAuthError,
    SentinelCircuitOpenError,
    SentinelClient,
    SentinelQuotaExceededError,
    SentinelRequestError,
    bands_evalscript,
    cloud_cache_key,
    cloud_evalscript,
    parse_cloud_statistics,
)
from app.shared.config import Settings
from tests.fixtures import geometries as g

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "sentinel"
SECRET = "fixture-client-secret-never-real"  # noqa: S105 - test fixture
TOKEN = json.loads((FIXTURES / "token_response.json").read_text())["access_token"]
BASE_URL = "https://sentinel.invalid"
TOKEN_URL = "https://identity.invalid/token"  # noqa: S105 - test fixture URL

PREDIO: BaseGeometry = shape(g.SQUARE)
SEPT_1, SEPT_30 = date(2026, 9, 1), date(2026, 9, 30)

Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "secret_key": "a-real-secret-key-with-at-least-32-chars",
        "totp_secret_encryption_key": "a-different-real-key-of-32-chars-min",
        "database_url": "postgresql+psycopg://lar_app:pw@localhost:5432/db",
        "database_migration_url": "postgresql+psycopg://lar_owner:pw@localhost:5432/db",
        "redis_url": "redis://localhost:6379/15",
        "sentinel_client_id": "fixture-client-id",
        "sentinel_client_secret": SECRET,
        "sentinel_base_url": BASE_URL,
        "sentinel_token_url": TOKEN_URL,
        "sentinel_max_retries": 2,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class FakeSentinel:
    """Routes requests by path to queued responses and records every call."""

    def __init__(self) -> None:
        """Start with no routes and no recorded calls."""
        self.calls: list[httpx.Request] = []
        self.routes: dict[str, list[httpx.Response] | Handler] = {}
        self.token_calls = 0

    def queue(self, path: str, *responses: httpx.Response) -> None:
        """Queue responses to be returned, in order, for ``path``."""
        self.routes.setdefault(path, [])
        route = self.routes[path]
        assert isinstance(route, list)
        route.extend(responses)

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        """Answer a request from the token route or the queued responses."""
        self.calls.append(request)
        if str(request.url) == TOKEN_URL:
            self.token_calls += 1
            route = self.routes.get("token")
            if callable(route):
                return await route(request)
            return httpx.Response(200, json=_fixture("token_response.json"))
        route = self.routes.get(request.url.path)
        if callable(route):
            return await route(request)
        if not route:
            raise AssertionError(f"unexpected request to {request.url.path}")
        return route.pop(0)

    def api_calls(self, path: str) -> int:
        """Return how many requests reached ``path``."""
        return sum(1 for call in self.calls if call.url.path == path)


class Events:
    """Records what the client reports to the audit trail."""

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.recorded: list[tuple[str, dict[str, object]]] = []

    async def __call__(self, event_type: str, details: dict[str, object]) -> None:
        """Record one event."""
        self.recorded.append((event_type, details))

    def types(self) -> list[str]:
        """Return the recorded event types, in order."""
        return [event for event, _ in self.recorded]


class Sleeps:
    """Stands in for asyncio.sleep: records the delays, waits for nothing."""

    def __init__(self) -> None:
        """Start with no delays recorded."""
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        """Record the delay instead of waiting it out."""
        self.delays.append(seconds)


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/15"))
    try:
        await client.ping()
    except RedisError:
        await client.aclose()
        if os.environ.get("CI"):
            raise
        pytest.skip("Redis is not reachable for the Sentinel client tests")
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def prefix(redis: Redis) -> AsyncIterator[str]:
    key_prefix = f"test-sh-{uuid.uuid4().hex[:8]}"
    yield key_prefix
    keys = [key async for key in redis.scan_iter(match=f"{key_prefix}:*")]
    if keys:
        await redis.delete(*keys)


@pytest.fixture
def fake() -> FakeSentinel:
    return FakeSentinel()


@pytest.fixture
def events() -> Events:
    return Events()


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


def _client(
    redis: Redis,
    prefix: str,
    fake: FakeSentinel,
    events: Events,
    sleeps: Sleeps,
    **overrides: Any,
) -> SentinelClient:
    return SentinelClient(
        _settings(**overrides),
        redis,
        events,
        http=httpx.AsyncClient(transport=httpx.MockTransport(fake)),
        sleep=sleeps,
        key_prefix=prefix,
    )


@pytest.fixture
def client(
    redis: Redis, prefix: str, fake: FakeSentinel, events: Events, sleeps: Sleeps
) -> SentinelClient:
    return _client(redis, prefix, fake, events, sleeps)


def _ok(path_payload: Any, pu: float | None = None) -> httpx.Response:
    headers = {PU_HEADER: str(pu)} if pu is not None else {}
    return httpx.Response(200, json=path_payload, headers=headers)


# --- construction -----------------------------------------------------------------


def test_the_client_refuses_to_exist_without_credentials(redis: Redis) -> None:
    with pytest.raises(SentinelAuthError, match="not configured"):
        SentinelClient(
            _settings(sentinel_client_id=None, sentinel_client_secret=None),
            redis,
            Events(),
        )


# --- token ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_token_is_cached_and_reused_within_its_ttl(
    client: SentinelClient, fake: FakeSentinel, redis: Redis, prefix: str
) -> None:
    assert await client.get_token() == TOKEN
    assert await client.get_token() == TOKEN
    assert fake.token_calls == 1
    # expires_in 600, minus the 60 s safety margin.
    assert 530 <= await redis.ttl(f"{prefix}:token") <= 540


@pytest.mark.asyncio
async def test_the_token_is_refreshed_when_it_expires(
    client: SentinelClient, fake: FakeSentinel, redis: Redis, prefix: str
) -> None:
    async def short_lived(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"access_token": f"token-{fake.token_calls}", "expires_in": 61}
        )

    fake.routes["token"] = short_lived
    first = await client.get_token()
    assert await redis.ttl(f"{prefix}:token") == 1
    await asyncio.sleep(1.2)  # let Redis expire it for real

    second = await client.get_token()
    assert (first, second) == ("token-1", "token-2")
    assert fake.token_calls == 2


@pytest.mark.asyncio
async def test_concurrent_requests_refresh_the_token_once(
    client: SentinelClient, fake: FakeSentinel
) -> None:
    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.3)  # long enough for every caller to pile up
        return httpx.Response(200, json=_fixture("token_response.json"))

    fake.routes["token"] = slow
    tokens = await asyncio.gather(*(client.get_token() for _ in range(10)))

    assert set(tokens) == {TOKEN}
    assert fake.token_calls == 1


@pytest.mark.asyncio
async def test_a_rejected_token_is_dropped_and_fetched_again_once(
    client: SentinelClient, fake: FakeSentinel
) -> None:
    fake.queue(CATALOG_PATH, httpx.Response(401), _ok({"features": [], "context": {}}))
    assert await client.search_scenes(PREDIO, SEPT_1, SEPT_30) == []
    assert fake.token_calls == 2


# --- processing units ------------------------------------------------------------


@pytest.mark.asyncio
async def test_processing_units_accumulate_per_month(
    client: SentinelClient, fake: FakeSentinel, redis: Redis, prefix: str
) -> None:
    fake.queue(
        STATISTICS_PATH,
        _ok(_fixture("statistics_cloud.json"), pu=1.5),
        _ok(_fixture("statistics_cloud.json"), pu=2.25),
    )
    await client.cloud_coverage_by_date(PREDIO, SEPT_1, SEPT_30)
    await client.cloud_coverage_by_date(PREDIO, SEPT_1, date(2026, 9, 29))  # no cache hit

    usage = await client.pu_usage()
    assert usage.spent == pytest.approx(3.75)
    assert usage.remaining == pytest.approx(30_000 - 3.75)
    key = f"{prefix}:pu:{usage.month}"
    assert await redis.ttl(key) > 30 * 24 * 3600  # outlives the month it counts


@pytest.mark.asyncio
async def test_a_spent_budget_refuses_billable_requests_without_calling_out(
    redis: Redis,
    prefix: str,
    fake: FakeSentinel,
    events: Events,
    sleeps: Sleeps,
) -> None:
    client = _client(redis, prefix, fake, events, sleeps, sentinel_monthly_pu_budget=10)
    usage = await client.pu_usage()
    await redis.set(f"{prefix}:pu:{usage.month}", "10.0")

    with pytest.raises(SentinelQuotaExceededError) as caught:
        await client.cloud_coverage_by_date(PREDIO, SEPT_1, SEPT_30)

    assert fake.api_calls(STATISTICS_PATH) == 0
    assert caught.value.as_dict() == {
        "error": "SentinelQuotaExceededError",
        "spent_pu": 10.0,
        "budget_pu": 10,
    }
    assert events.types() == [SENTINEL_QUOTA_EXCEEDED]


@pytest.mark.asyncio
async def test_the_free_catalog_still_answers_when_the_budget_is_spent(
    redis: Redis,
    prefix: str,
    fake: FakeSentinel,
    events: Events,
    sleeps: Sleeps,
) -> None:
    client = _client(redis, prefix, fake, events, sleeps, sentinel_monthly_pu_budget=10)
    usage = await client.pu_usage()
    await redis.set(f"{prefix}:pu:{usage.month}", "10.0")
    fake.queue(CATALOG_PATH, _ok(_fixture("catalog_search.json")))

    assert len(await client.search_scenes(PREDIO, SEPT_1, SEPT_30)) == 5


# --- cloud statistics and its cache ----------------------------------------------


@pytest.mark.asyncio
async def test_the_cloud_measurement_is_cached(
    client: SentinelClient, fake: FakeSentinel, redis: Redis, prefix: str
) -> None:
    fake.queue(STATISTICS_PATH, _ok(_fixture("statistics_cloud.json"), pu=0.5))
    first = await client.cloud_coverage_by_date(PREDIO, SEPT_1, SEPT_30)
    second = await client.cloud_coverage_by_date(PREDIO, SEPT_1, SEPT_30)

    assert first == second
    assert fake.api_calls(STATISTICS_PATH) == 1
    key = cloud_cache_key(prefix, PREDIO, SEPT_1, SEPT_30)
    assert key.startswith(f"{prefix}:cloud:") and key.endswith(":2026-09-01:2026-09-30")
    assert 21_590 <= await redis.ttl(key) <= 21_600


@pytest.mark.asyncio
async def test_the_statistical_request_measures_scl_over_the_exact_geometry(
    client: SentinelClient, fake: FakeSentinel
) -> None:
    fake.queue(STATISTICS_PATH, _ok(_fixture("statistics_cloud.json")))
    await client.cloud_coverage_by_date(PREDIO, SEPT_1, SEPT_30)

    body = json.loads(fake.calls[-1].content)
    assert body["input"]["bounds"]["geometry"]["type"] == "Polygon"
    assert body["aggregation"]["aggregationInterval"] == {"of": "P1D"}
    # 60 m in degrees at the predio's latitude: ~0.00054 deg of latitude.
    assert body["aggregation"]["resy"] == pytest.approx(60 / 110_574)


def test_cloud_statistics_parse_into_fractions_per_date() -> None:
    observations = parse_cloud_statistics(_fixture("statistics_cloud.json"))
    assert [(o.acquired_on.isoformat(), o.cloud_fraction) for o in observations] == [
        ("2026-09-10", 0.0),
        ("2026-09-15", 0.12),
        ("2026-09-20", 0.85),
    ]


def test_a_date_without_valid_pixels_has_no_fraction_rather_than_zero() -> None:
    # 0 would read as "perfectly clear"; no data is not clear.
    payload = {
        "data": [
            {
                "interval": {"from": "2026-09-10T00:00:00Z"},
                "outputs": {
                    "cloud": {
                        "bands": {
                            "B0": {
                                "stats": {
                                    "mean": "NaN",
                                    "sampleCount": 300,
                                    "noDataCount": 300,
                                }
                            }
                        }
                    }
                },
            },
            {
                "interval": {"from": "2026-09-11T00:00:00Z"},
                "error": {"type": "EXECUTION"},
            },
        ]
    }
    [observation] = parse_cloud_statistics(payload)
    assert (observation.cloud_fraction, observation.valid_pixels) == (None, 0)


def test_the_cloud_evalscript_counts_shadow_cloud_and_cirrus() -> None:
    script = cloud_evalscript()
    for scl_class in (3, 8, 9, 10):
        assert f"s.SCL == {scl_class}" in script
    assert "s.SCL != 0" in script  # no-data is excluded, not counted as clear


# --- scene selection -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_selection_picks_the_most_recent_date_under_the_threshold(
    client: SentinelClient, fake: FakeSentinel
) -> None:
    fake.queue(CATALOG_PATH, _ok(_fixture("catalog_search.json")))
    fake.queue(STATISTICS_PATH, _ok(_fixture("statistics_cloud.json")))

    selection = await client.select_scene(PREDIO, SEPT_1, SEPT_30)

    # 09-20 is newer and its *tile* looked fine (12.4%), but over the predio
    # it was 85% cloudy. 09-15 is the most recent date clear over the predio.
    assert selection.scene_date == date(2026, 9, 15)
    assert selection.cloud_coverage == pytest.approx(0.12)
    assert selection.scene_id == (
        "S2A_MSIL2A_20260915T143731_N0511_R096_T19HCC_20260915T191008"
        "+S2A_MSIL2A_20260915T143731_N0511_R096_T19HCD_20260915T191008"
    )
    assert [o.acquired_on.day for o in selection.evaluated] == [20, 15, 10]


@pytest.mark.asyncio
async def test_the_statistical_span_is_narrowed_to_the_candidates(
    client: SentinelClient, fake: FakeSentinel
) -> None:
    fake.queue(CATALOG_PATH, _ok(_fixture("catalog_search.json")))
    fake.queue(STATISTICS_PATH, _ok(_fixture("statistics_cloud.json")))
    await client.select_scene(PREDIO, SEPT_1, SEPT_30)

    stats_request = next(c for c in fake.calls if c.url.path == STATISTICS_PATH)
    time_range = json.loads(stats_request.content)["aggregation"]["timeRange"]
    # The 100% cloudy tile of 09-05 is dropped by the free pre-filter.
    assert time_range == {"from": "2026-09-10T00:00:00Z", "to": "2026-09-20T23:59:59Z"}


@pytest.mark.asyncio
async def test_no_suitable_scene_lists_every_date_evaluated(
    redis: Redis,
    prefix: str,
    fake: FakeSentinel,
    events: Events,
    sleeps: Sleeps,
) -> None:
    client = _client(
        redis, prefix, fake, events, sleeps, analysis_max_cloud_coverage=0.05
    )
    stats = _fixture("statistics_cloud.json")
    stats["data"][0]["outputs"]["cloud"]["bands"]["B0"]["stats"]["mean"] = 0.47
    fake.queue(CATALOG_PATH, _ok(_fixture("catalog_search.json")))
    fake.queue(STATISTICS_PATH, _ok(stats))

    with pytest.raises(NoSuitableSceneFoundError) as caught:
        await client.select_scene(PREDIO, SEPT_1, SEPT_30)

    detail = caught.value.as_dict()
    assert detail["scenes_evaluated"] == 3
    assert detail["best_cloud_fraction"] == pytest.approx(0.12)
    assert [entry["date"] for entry in detail["evaluated"]] == [  # type: ignore[union-attr]
        "2026-09-20",
        "2026-09-15",
        "2026-09-10",
    ]
    assert detail["tile_level_rejections"][0]["tile_cloud_cover"] == 100.0  # type: ignore[index]


@pytest.mark.asyncio
async def test_all_tiles_fully_cloudy_spends_no_processing_units(
    client: SentinelClient, fake: FakeSentinel
) -> None:
    catalog = _fixture("catalog_search.json")
    for feature in catalog["features"]:
        feature["properties"]["eo:cloud_cover"] = 100.0
    fake.queue(CATALOG_PATH, _ok(catalog))

    with pytest.raises(NoSuitableSceneFoundError) as caught:
        await client.select_scene(PREDIO, SEPT_1, SEPT_30)

    assert fake.api_calls(STATISTICS_PATH) == 0
    assert len(caught.value.tile_level_rejections) == 5


@pytest.mark.asyncio
async def test_catalog_pagination_follows_next(
    client: SentinelClient, fake: FakeSentinel
) -> None:
    page = _fixture("catalog_search.json")
    first = {**page, "features": page["features"][:2], "context": {"next": 2}}
    second = {**page, "features": page["features"][2:], "context": {}}
    fake.queue(CATALOG_PATH, _ok(first), _ok(second))

    scenes = await client.search_scenes(PREDIO, SEPT_1, SEPT_30)
    assert len(scenes) == 5
    assert json.loads(fake.calls[-1].content)["next"] == 2


# --- process ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_bands_asks_for_harmonised_reflectance_and_nearest_scl(
    client: SentinelClient, fake: FakeSentinel
) -> None:
    fake.queue(
        PROCESS_PATH,
        httpx.Response(
            200,
            content=b"II*\x00tiff",
            headers={"content-type": "image/tiff", PU_HEADER: "3.2"},
        ),
    )
    response = await client.fetch_bands(
        PREDIO, date(2026, 9, 15), date(2026, 9, 15), ["B04", "B08", "SCL"]
    )

    body = json.loads(fake.calls[-1].content)
    data = body["input"]["data"][0]
    assert data["processing"] == {
        "harmonizeValues": True,
        "upsampling": "NEAREST",
        "downsampling": "NEAREST",
    }
    assert data["dataFilter"]["timeRange"] == {
        "from": "2026-09-15T00:00:00Z",
        "to": "2026-09-15T23:59:59Z",
    }
    assert '"REFLECTANCE", "REFLECTANCE", "DN", "DN"' in body["evalscript"]
    assert response.bands == ("B04", "B08", "SCL", "dataMask")
    assert response.processing_units == pytest.approx(3.2)
    # The bbox is the predio's, grown by a margin.
    min_x, min_y, max_x, max_y = PREDIO.bounds
    assert response.bbox[0] < min_x and response.bbox[3] > max_y


@pytest.mark.parametrize("bands", [["B04", "B02"], [], ["B04", "B04"], ['B04"]); x(']])
def test_the_evalscript_only_accepts_allowlisted_bands(bands: list[str]) -> None:
    with pytest.raises(SentinelRequestError):
        bands_evalscript(bands)


@pytest.mark.asyncio
async def test_a_predio_too_large_for_one_request_is_refused(
    client: SentinelClient, fake: FakeSentinel
) -> None:
    huge = shape(g.square(size=0.5))  # ~45 x 55 km: beyond 2500 px at 10 m
    with pytest.raises(SentinelRequestError, match="too large"):
        await client.fetch_bands(huge, SEPT_1, SEPT_1, ["B04", "B08", "SCL"])
    assert fake.api_calls(PROCESS_PATH) == 0


# --- resilience ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_503_is_retried(
    client: SentinelClient, fake: FakeSentinel, sleeps: Sleeps
) -> None:
    fake.queue(CATALOG_PATH, httpx.Response(503), _ok({"features": [], "context": {}}))
    assert await client.search_scenes(PREDIO, SEPT_1, SEPT_30) == []
    assert fake.api_calls(CATALOG_PATH) == 2
    assert len(sleeps.delays) == 1


@pytest.mark.asyncio
async def test_backoff_grows_exponentially(
    client: SentinelClient, fake: FakeSentinel, sleeps: Sleeps
) -> None:
    fake.queue(CATALOG_PATH, *(httpx.Response(503) for _ in range(3)))
    with pytest.raises(SentinelRequestError, match="gave up after 3 attempts"):
        await client.search_scenes(PREDIO, SEPT_1, SEPT_30)
    first, second = sleeps.delays
    assert 1.0 <= first <= 1.25 and 2.0 <= second <= 2.5


@pytest.mark.asyncio
async def test_a_400_is_not_retried(
    client: SentinelClient, fake: FakeSentinel, sleeps: Sleeps, redis: Redis, prefix: str
) -> None:
    fake.queue(CATALOG_PATH, httpx.Response(400, json={"error": "bad bbox"}))
    with pytest.raises(SentinelRequestError) as caught:
        await client.search_scenes(PREDIO, SEPT_1, SEPT_30)
    assert caught.value.status_code == 400
    assert fake.api_calls(CATALOG_PATH) == 1
    assert sleeps.delays == []
    # A client error says nothing about the service's health.
    assert await redis.get(f"{prefix}:circuit:failures") is None


@pytest.mark.asyncio
async def test_a_429_waits_the_retry_after_in_milliseconds(
    client: SentinelClient, fake: FakeSentinel, sleeps: Sleeps
) -> None:
    fake.queue(
        CATALOG_PATH,
        httpx.Response(429, headers={"retry-after": "1500"}),
        _ok({"features": [], "context": {}}),
    )
    await client.search_scenes(PREDIO, SEPT_1, SEPT_30)
    # Sentinel Hub's Retry-After is milliseconds: 1500 means 1.5 s, not 25 min.
    assert sleeps.delays == [1.5]


@pytest.mark.asyncio
async def test_timeouts_are_retried(
    client: SentinelClient, fake: FakeSentinel, sleeps: Sleeps
) -> None:
    attempts = 0

    async def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return _ok({"features": [], "context": {}})

    fake.routes[CATALOG_PATH] = flaky
    assert await client.search_scenes(PREDIO, SEPT_1, SEPT_30) == []
    assert attempts == 2


@pytest.mark.asyncio
async def test_the_circuit_opens_after_n_failures_and_closes_after_the_period(
    redis: Redis,
    prefix: str,
    fake: FakeSentinel,
    events: Events,
    sleeps: Sleeps,
) -> None:
    client = _client(
        redis,
        prefix,
        fake,
        events,
        sleeps,
        sentinel_max_retries=0,
        sentinel_circuit_failure_threshold=2,
        sentinel_circuit_reset_s=1,
    )
    fake.queue(CATALOG_PATH, httpx.Response(503), httpx.Response(503))
    for _ in range(2):
        with pytest.raises(SentinelRequestError):
            await client.search_scenes(PREDIO, SEPT_1, SEPT_30)
    assert events.types() == [SENTINEL_CIRCUIT_OPEN]

    # Open: fails at once, without touching the network.
    with pytest.raises(SentinelCircuitOpenError):
        await client.search_scenes(PREDIO, SEPT_1, SEPT_30)
    assert fake.api_calls(CATALOG_PATH) == 2

    await asyncio.sleep(1.2)  # the open period expires in Redis
    fake.queue(CATALOG_PATH, _ok({"features": [], "context": {}}))
    assert await client.search_scenes(PREDIO, SEPT_1, SEPT_30) == []
    assert await redis.get(f"{prefix}:circuit:failures") is None


# --- secrets ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_token_and_secret_never_reach_the_logs(
    client: SentinelClient,
    fake: FakeSentinel,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    fake.queue(
        CATALOG_PATH,
        httpx.Response(401),  # forces a refresh, which is logged
        httpx.Response(400, json={"echo": TOKEN}),  # a body echoing the token
    )
    with pytest.raises(SentinelRequestError) as caught:
        await client.search_scenes(PREDIO, SEPT_1, SEPT_30)

    async def refused(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client", "secret": SECRET})

    fake.routes["token"] = refused
    await client._forget_token()
    with pytest.raises(SentinelAuthError) as auth_failure:
        await client.get_token()

    captured = capsys.readouterr()
    everything = "\n".join(
        [
            caplog.text,
            captured.out,
            captured.err,
            str(caught.value),
            repr(caught.value),
            str(auth_failure.value),
            repr(auth_failure.value),
        ]
    )
    assert TOKEN not in everything
    assert SECRET not in everything
