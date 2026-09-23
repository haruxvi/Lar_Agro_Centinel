"""Security headers, request context and rate limiting."""

from __future__ import annotations

import uuid

import pytest
import redis
from fastapi.testclient import TestClient

from app.shared.middleware import REQUEST_ID_HEADER, SECURITY_HEADERS
from app.shared.rate_limit import limiter


@pytest.mark.parametrize(("header", "value"), sorted(SECURITY_HEADERS.items()))
def test_every_security_header_is_present(
    client: TestClient, header: str, value: str
) -> None:
    response = client.get("/health")
    assert response.headers[header] == value


def test_security_headers_are_present_on_error_responses(client: TestClient) -> None:
    # A 404 is still a response an attacker can frame or sniff.
    response = client.get("/does-not-exist")
    assert response.status_code == 404
    for header, value in SECURITY_HEADERS.items():
        assert response.headers[header] == value


def test_clickjacking_and_sniffing_are_denied(client: TestClient) -> None:
    headers = client.get("/health").headers
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "max-age=31536000" in headers["Strict-Transport-Security"]


def test_each_request_gets_its_own_request_id(client: TestClient) -> None:
    first = client.get("/health").headers[REQUEST_ID_HEADER]
    second = client.get("/health").headers[REQUEST_ID_HEADER]

    assert uuid.UUID(first)
    assert uuid.UUID(second)
    assert first != second


def test_request_id_is_returned_on_errors_too(client: TestClient) -> None:
    response = client.get("/does-not-exist")
    assert uuid.UUID(response.headers[REQUEST_ID_HEADER])


def test_exceeding_the_login_limit_returns_429(
    api_client: TestClient, redis_client: redis.Redis
) -> None:
    # The limiter is off for the rest of the suite, so it does not throttle
    # tests whose subject is something else.
    redis_client.flushdb()
    limiter.enabled = True
    try:
        statuses = [
            api_client.post(
                "/api/v1/auth/login",
                json={"email": "nobody@example.cl", "password": "whatever-1234"},
            ).status_code
            for _ in range(7)
        ]
    finally:
        limiter.enabled = False
        redis_client.flushdb()

    assert 429 in statuses, statuses
    # The limit bites after the configured number of attempts, not before.
    assert statuses[0] == 401


def test_a_rate_limited_response_still_carries_security_headers(
    api_client: TestClient, redis_client: redis.Redis
) -> None:
    redis_client.flushdb()
    limiter.enabled = True
    try:
        response = None
        for _ in range(7):
            response = api_client.post(
                "/api/v1/auth/login",
                json={"email": "nobody@example.cl", "password": "whatever-1234"},
            )
            if response.status_code == 429:
                break
    finally:
        limiter.enabled = False
        redis_client.flushdb()

    assert response is not None
    assert response.status_code == 429
    assert response.headers["X-Frame-Options"] == "DENY"
    assert uuid.UUID(response.headers[REQUEST_ID_HEADER])


def test_a_successful_response_works_while_the_limiter_is_enabled(
    api_client: TestClient, redis_client: redis.Redis
) -> None:
    # slowapi injects its headers into the endpoint's `response` object. An
    # endpoint that does not declare one raises at runtime, and only on the
    # success path: errors and 429s never reach the injection.
    redis_client.flushdb()
    limiter.enabled = True
    try:
        response = api_client.post(
            "/api/v1/auth/register",
            json={
                "email": f"limited-{uuid.uuid4().hex[:8]}@example.cl",
                "password": "Cordillera-Sur-2026",
                "full_name": "Rate Limited",
            },
        )
    finally:
        limiter.enabled = False
        redis_client.flushdb()

    assert response.status_code == 201, response.text
    assert "x-ratelimit-limit" in {key.lower() for key in response.headers}
