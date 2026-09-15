"""Application factory wiring."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import API_PREFIX, create_app


def test_create_app_exposes_metadata() -> None:
    app = create_app()
    assert app.title == "Lar Agro Centinel"
    assert app.version == "0.1.0"


def test_openapi_schema_is_generated(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert response.json()["info"]["title"] == "Lar Agro Centinel"


def test_module_routers_are_mounted_under_api_prefix() -> None:
    app = create_app()
    prefixes = {
        route.path
        for route in app.routes
        if getattr(route, "path", "").startswith(API_PREFIX)
    }
    assert all(path.startswith("/api/v1/") for path in prefixes)
