"""Health endpoint contract."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_returns_healthy(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["version"] == "0.1.0"
    assert body["uptime_seconds"] >= 0
    assert isinstance(body["checks"], dict)
