"""Role catalogue endpoint contract."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.shared.roles import ROLE_META, ROLES_2FA_REQUIRED, Role


def test_roles_endpoint_returns_every_role(client: TestClient) -> None:
    response = client.get("/api/meta/roles")
    assert response.status_code == 200
    roles = response.json()["roles"]
    assert len(roles) == 9
    assert {entry["role"] for entry in roles} == {role.value for role in Role}


def test_roles_endpoint_exposes_both_labels(client: TestClient) -> None:
    roles = client.get("/api/meta/roles").json()["roles"]
    by_role = {entry["role"]: entry for entry in roles}

    admin = by_role[Role.ADMIN_OPERACIONES.value]
    assert admin["label"] == ROLE_META[Role.ADMIN_OPERACIONES].label
    assert admin["short_label"] == ROLE_META[Role.ADMIN_OPERACIONES].short_label
    assert admin["description"]
    assert admin["color"]


def test_roles_endpoint_flags_two_factor_requirement(client: TestClient) -> None:
    roles = client.get("/api/meta/roles").json()["roles"]
    flagged = {entry["role"] for entry in roles if entry["requires_two_factor"]}
    assert flagged == {role.value for role in ROLES_2FA_REQUIRED}


def test_roles_endpoint_is_public(client: TestClient) -> None:
    # No Authorization header is sent by the fixture client.
    assert client.get("/api/meta/roles").status_code == 200
