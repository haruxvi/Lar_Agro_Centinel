"""Health endpoint: real dependency checks, no infrastructure disclosure."""

from __future__ import annotations

import pytest
import redis
from fastapi.testclient import TestClient

from app.modules.health import service as health_service
from app.modules.health.service import HealthService


def test_a_healthy_service_reports_every_dependency_up(
    api_client: TestClient, redis_client: redis.Redis
) -> None:
    response = api_client.get("/health")
    assert response.status_code == 200
    body = response.json()

    assert body["status"] == "healthy"
    assert body["checks"] == {"database": True, "redis": True}
    assert body["version"] == "0.1.0"
    assert body["uptime_seconds"] >= 0


def test_losing_the_cache_degrades_the_service_without_taking_it_down(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _unreachable() -> redis.Redis:
        raise redis.ConnectionError("redis is down")

    monkeypatch.setattr(health_service, "get_redis", _unreachable)

    response = api_client.get("/health")
    # Rate limiting and caching suffer, but requests are still served, so the
    # load balancer must keep sending traffic here.
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["checks"] == {"database": True, "redis": False}


def test_losing_the_database_makes_the_service_unhealthy(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(HealthService, "check_database", lambda self: False)

    response = api_client.get("/health")
    assert response.status_code == 503
    assert response.json()["status"] == "unhealthy"
    assert response.json()["checks"]["database"] is False


def test_the_report_discloses_nothing_about_the_infrastructure(
    api_client: TestClient, redis_client: redis.Redis
) -> None:
    body = api_client.get("/health").json()

    assert set(body) == {"status", "checks", "version", "uptime_seconds"}
    assert set(body["checks"]) == {"database", "redis"}
    # No connection strings, hostnames, library versions or error text.
    rendered = api_client.get("/health").text.lower()
    for leak in ["postgres", "psycopg", "localhost", "password", "sqlalchemy", "5432"]:
        assert leak not in rendered


def test_health_needs_no_authentication(
    api_client: TestClient, redis_client: redis.Redis
) -> None:
    assert api_client.get("/health").status_code == 200
