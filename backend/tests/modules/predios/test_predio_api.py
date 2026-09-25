"""HTTP surface of predios and lotes, end to end through the runtime role."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.audit import events
from app.modules.audit.models import AuditLog
from app.modules.predios.models import Lote
from app.modules.users.models import User, UserPredioRole, UserRole
from app.shared.geo import max_geojson_bytes
from app.shared.roles import Role
from app.shared.security import create_access_token
from tests.fixtures import geometries as g

BASE = "/api/v1/predios"
HALF = g.STEP / 2
QUARTER = g.STEP / 4

Headers = dict[str, str]
MakeClientUser = Callable[..., tuple[User, Headers]]


@pytest.fixture
def make_client_user(app_session: Session) -> MakeClientUser:
    """Return a factory for users visible to the API, with their auth headers."""

    def _make(
        global_roles: tuple[Role, ...] = (),
        predio_roles: tuple[tuple[uuid.UUID, Role], ...] = (),
    ) -> tuple[User, Headers]:
        user = User(
            email=f"api-{uuid.uuid4().hex[:8]}@example.cl",
            password_hash="$argon2id$placeholder",
            full_name="API User",
        )
        app_session.add(user)
        app_session.flush()
        for role in global_roles:
            app_session.add(UserRole(user_id=user.id, role=role))
        for predio_id, role in predio_roles:
            app_session.add(
                UserPredioRole(user_id=user.id, predio_id=predio_id, role=role)
            )
        app_session.flush()
        roles = [*global_roles, *(role for _, role in predio_roles)]
        token = create_access_token(
            user.id,
            roles,
            roles[0] if roles else None,
            [predio_id for predio_id, _ in predio_roles],
            two_factor_verified=True,
        )
        return user, {"Authorization": f"Bearer {token}"}

    return _make


@pytest.fixture
def owner(make_client_user: MakeClientUser) -> tuple[User, Headers]:
    return make_client_user((Role.PROPIETARIO,))


@pytest.fixture
def headers(owner: tuple[User, Headers]) -> Headers:
    return owner[1]


def _create_predio(
    client: TestClient,
    headers: Headers,
    geometry: dict[str, Any] | None = None,
    name: str = "Viña Santa Elena",
) -> dict[str, Any]:
    response = client.post(
        BASE, json={"name": name, "geometry": geometry or g.SQUARE}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()["predio"]


def _create_lote(
    client: TestClient,
    headers: Headers,
    predio_id: str,
    geometry: dict[str, Any] | None = None,
    name: str = "Cuartel 1",
) -> Any:
    return client.post(
        f"{BASE}/{predio_id}/lotes",
        json={
            "name": name,
            "lote_type": "CUARTEL",
            "geometry": geometry or g.square(size=QUARTER),
        },
        headers=headers,
    )


# --- predios ------------------------------------------------------------------


def test_create_returns_the_predio_with_its_geometry(
    api_client: TestClient, headers: Headers, app_session: Session
) -> None:
    predio = _create_predio(api_client, headers)

    assert predio["slug"] == "vina-santa-elena"
    assert predio["area_ha"] == pytest.approx(101.7, rel=0.01)
    assert predio["geometry"]["type"] == "Polygon"
    audited = app_session.execute(
        select(func.count()).where(
            AuditLog.event_type == events.PREDIO_CREATED,
            AuditLog.predio_id == uuid.UUID(predio["id"]),
        )
    ).scalar_one()
    assert audited == 1


def test_create_requires_the_predio_create_permission(
    api_client: TestClient, make_client_user: MakeClientUser
) -> None:
    _, agronomo = make_client_user((Role.AGRONOMO,))
    response = api_client.post(
        BASE, json={"name": "X", "geometry": g.SQUARE}, headers=agronomo
    )
    assert response.status_code == 403


def test_create_rejects_an_invalid_geometry_with_its_reason(
    api_client: TestClient, headers: Headers
) -> None:
    response = api_client.post(
        BASE, json={"name": "X", "geometry": g.LINESTRING}, headers=headers
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "InvalidGeometryError"
    assert detail["received"] == "LineString"


def test_create_rejects_unknown_fields(api_client: TestClient, headers: Headers) -> None:
    response = api_client.post(
        BASE,
        json={"name": "X", "geometry": g.SQUARE, "owner_user_id": str(uuid.uuid4())},
        headers=headers,
    )
    assert response.status_code == 422


def test_listing_carries_no_geometry(api_client: TestClient, headers: Headers) -> None:
    _create_predio(api_client, headers)
    body = api_client.get(BASE, headers=headers).json()

    assert body["total"] == 1
    [item] = body["items"]
    assert "geometry" not in item
    assert item["centroid"] == pytest.approx([g.BASE_LON + HALF, g.BASE_LAT + HALF])


def test_listing_caps_the_page_size(api_client: TestClient, headers: Headers) -> None:
    body = api_client.get(BASE, params={"page_size": 1000}, headers=headers).json()
    assert body["page_size"] == 100


def test_a_stranger_gets_403_on_someone_elses_predio(
    api_client: TestClient, headers: Headers, make_client_user: MakeClientUser
) -> None:
    predio = _create_predio(api_client, headers)
    _, stranger = make_client_user((Role.PROPIETARIO,))

    assert api_client.get(f"{BASE}/{predio['id']}", headers=stranger).status_code == 403
    # An id that does not exist answers the same: existence is not disclosed.
    assert api_client.get(f"{BASE}/{uuid.uuid4()}", headers=stranger).status_code == 403


def test_an_aplicador_can_read_but_not_edit(
    api_client: TestClient, headers: Headers, make_client_user: MakeClientUser
) -> None:
    predio = _create_predio(api_client, headers)
    _, aplicador = make_client_user(
        predio_roles=((uuid.UUID(predio["id"]), Role.APLICADOR),)
    )
    url = f"{BASE}/{predio['id']}"

    assert api_client.get(url, headers=aplicador).status_code == 200
    assert (
        api_client.patch(url, json={"name": "Mío"}, headers=aplicador).status_code == 403
    )
    assert api_client.delete(url, headers=aplicador).status_code == 403


def test_patch_changes_the_boundary_and_reports_warnings(
    api_client: TestClient, headers: Headers
) -> None:
    predio = _create_predio(api_client, headers)
    response = api_client.patch(
        f"{BASE}/{predio['id']}",
        json={"geometry": g.OUTSIDE_CHILE},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert [w["code"] for w in response.json()["warnings"]] == ["OUTSIDE_CHILE_BBOX"]


def test_patch_cannot_null_the_name(api_client: TestClient, headers: Headers) -> None:
    predio = _create_predio(api_client, headers)
    response = api_client.patch(
        f"{BASE}/{predio['id']}", json={"name": None}, headers=headers
    )
    assert response.status_code == 422


def test_a_predio_with_lotes_cannot_be_deleted(
    api_client: TestClient, headers: Headers
) -> None:
    predio = _create_predio(api_client, headers)
    assert _create_lote(api_client, headers, predio["id"]).status_code == 201

    response = api_client.delete(f"{BASE}/{predio['id']}", headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "error": "PredioHasActiveLotesError",
        "active_lotes": 1,
    }


def test_delete_then_get_is_404(api_client: TestClient, headers: Headers) -> None:
    predio = _create_predio(api_client, headers)
    assert api_client.delete(f"{BASE}/{predio['id']}", headers=headers).status_code == 204
    assert api_client.get(f"{BASE}/{predio['id']}", headers=headers).status_code == 404


def test_summary(api_client: TestClient, headers: Headers) -> None:
    predio = _create_predio(api_client, headers)
    _create_lote(api_client, headers, predio["id"])

    summary = api_client.get(f"{BASE}/{predio['id']}/summary", headers=headers).json()
    assert summary["lotes_count"] == 1
    assert summary["coverage_ratio"] == pytest.approx(1 / 16, rel=0.01)
    assert summary["lotes_by_type"] == {"CUARTEL": 1}


# --- geometry validation ------------------------------------------------------


def test_validate_geometry_measures_without_storing(
    api_client: TestClient, headers: Headers
) -> None:
    response = api_client.post(
        f"{BASE}/validate-geometry", json={"geometry": g.SQUARE}, headers=headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["vertex_count"] == 5
    assert body["area_m2"] == pytest.approx(1_017_000, rel=0.01)
    assert api_client.get(BASE, headers=headers).json()["total"] == 0


def test_validate_geometry_rejects_what_cannot_be_stored(
    api_client: TestClient, headers: Headers
) -> None:
    # A spike used to be the example; it is now repairable (approved change).
    # A line is not a polygon under any repair.
    response = api_client.post(
        f"{BASE}/validate-geometry", json={"geometry": g.LINESTRING}, headers=headers
    )
    assert response.status_code == 422


def test_validate_geometry_always_returns_the_repair_preview(
    api_client: TestClient, headers: Headers
) -> None:
    above = g.protruding_hole(side_m=1000, hole_height_m=200, inside_m=50, outside_m=100)
    response = api_client.post(
        f"{BASE}/validate-geometry", json={"geometry": above}, headers=headers
    )

    # 200 even over the threshold: this endpoint exists to preview before saving.
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["repaired"] is True
    assert body["geometry"]["type"] == "Polygon"
    repair = body["repair"]
    assert repair["requires_confirmation"] is True
    assert repair["reason"] == "AREA_CHANGE_ABOVE_THRESHOLD"
    assert repair["area_change_m2"] == pytest.approx(20_026, rel=0.01)
    assert repair["area_before_m2"] < repair["area_after_m2"]


def test_validate_geometry_reports_unmeasurable_repairs_with_a_null_ratio(
    api_client: TestClient, headers: Headers
) -> None:
    response = api_client.post(
        f"{BASE}/validate-geometry",
        json={"geometry": g.ASYMMETRIC_BOWTIE},
        headers=headers,
    )
    repair = response.json()["repair"]
    assert repair["reason"] == "NOT_MEASURABLE_SELF_INTERSECTION"
    assert repair["area_change_ratio"] is None
    assert repair["requires_confirmation"] is True


def test_a_valid_geometry_validates_without_a_repair(
    api_client: TestClient, headers: Headers
) -> None:
    body = api_client.post(
        f"{BASE}/validate-geometry", json={"geometry": g.SQUARE}, headers=headers
    ).json()
    assert (body["repaired"], body["repair"]) == (False, None)
    assert body["geometry"]["type"] == "Polygon"


# --- payload size -------------------------------------------------------------


def _oversized_body() -> bytes:
    padding = "x" * (max_geojson_bytes() + 1)
    return json.dumps({"name": "X", "geometry": g.SQUARE, "pad": padding}).encode()


def test_an_oversized_body_is_refused_with_413(
    api_client: TestClient, headers: Headers
) -> None:
    response = api_client.post(
        BASE,
        content=_oversized_body(),
        headers={**headers, "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["detail"]["limit_bytes"] == max_geojson_bytes()


def test_an_oversized_body_without_content_length_is_refused(
    api_client: TestClient, headers: Headers
) -> None:
    body = _oversized_body()

    def chunks() -> Iterator[bytes]:
        for start in range(0, len(body), 64 * 1024):
            yield body[start : start + 64 * 1024]

    response = api_client.post(
        BASE,
        content=chunks(),
        headers={**headers, "Content-Type": "application/json"},
    )
    assert response.status_code == 413


# --- lotes --------------------------------------------------------------------


def test_lotes_are_listed_without_geometry_and_read_with_it(
    api_client: TestClient, headers: Headers
) -> None:
    predio = _create_predio(api_client, headers)
    lote = _create_lote(api_client, headers, predio["id"]).json()["lote"]

    [item] = api_client.get(f"{BASE}/{predio['id']}/lotes", headers=headers).json()
    assert "geometry" not in item
    detail = api_client.get(f"{BASE}/{predio['id']}/lotes/{lote['id']}", headers=headers)
    assert detail.json()["geometry"]["type"] == "Polygon"


def test_a_lote_outside_the_predio_is_422(
    api_client: TestClient, headers: Headers
) -> None:
    predio = _create_predio(api_client, headers)
    response = _create_lote(
        api_client, headers, predio["id"], g.square(lon=g.BASE_LON - 0.05, size=QUARTER)
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "LoteNotContainedError"


def test_overlapping_lotes_are_422(api_client: TestClient, headers: Headers) -> None:
    predio = _create_predio(api_client, headers)
    _create_lote(api_client, headers, predio["id"])
    response = _create_lote(
        api_client,
        headers,
        predio["id"],
        g.square(lon=g.BASE_LON + QUARTER / 2, size=QUARTER),
        name="Cuartel 2",
    )
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "LotesOverlapError"


def test_a_lote_of_another_predio_is_404_not_403(
    api_client: TestClient, headers: Headers
) -> None:
    home = _create_predio(api_client, headers)
    other = _create_predio(
        api_client, headers, g.square(lon=g.BASE_LON + 0.05), name="Otro"
    )
    lote = _create_lote(api_client, headers, home["id"]).json()["lote"]

    url = f"{BASE}/{other['id']}/lotes/{lote['id']}"
    assert api_client.get(url, headers=headers).status_code == 404
    assert (
        api_client.patch(url, json={"name": "Robado"}, headers=headers).status_code == 404
    )
    assert api_client.delete(url, headers=headers).status_code == 404


def test_import_is_all_or_nothing(
    api_client: TestClient, headers: Headers, app_session: Session
) -> None:
    predio = _create_predio(api_client, headers)
    url = f"{BASE}/{predio['id']}/lotes/import-geojson"

    def feature(geometry: dict[str, Any], name: str) -> dict[str, Any]:
        return {"type": "Feature", "geometry": geometry, "properties": {"name": name}}

    rejected = api_client.post(
        url,
        json={
            "type": "FeatureCollection",
            "features": [
                feature(g.square(size=QUARTER), "A"),
                feature(g.square(lon=g.BASE_LON - 0.05, size=QUARTER), "Fuera"),
            ],
        },
        headers=headers,
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["features"][0]["index"] == 1
    stored = app_session.execute(
        select(func.count()).where(Lote.predio_id == uuid.UUID(predio["id"]))
    ).scalar_one()
    assert stored == 0

    accepted = api_client.post(
        url,
        json={
            "type": "FeatureCollection",
            "features": [
                feature(g.square(size=QUARTER), "A"),
                feature(g.square(lon=g.BASE_LON + HALF, size=QUARTER), "B"),
            ],
        },
        headers=headers,
    )
    assert accepted.status_code == 201, accepted.text
    assert [lote["name"] for lote in accepted.json()["created"]] == ["A", "B"]


# --- membership ---------------------------------------------------------------


def test_members_are_assigned_listed_and_revoked(
    api_client: TestClient,
    owner: tuple[User, Headers],
    make_client_user: MakeClientUser,
) -> None:
    user, headers = owner
    predio = _create_predio(api_client, headers)
    worker, _ = make_client_user()
    url = f"{BASE}/{predio['id']}/users"

    created = api_client.post(
        url, json={"email": worker.email, "role": "APLICADOR"}, headers=headers
    )
    assert created.status_code == 201, created.text
    assert created.json()["user_id"] == str(worker.id)

    members = {
        (m["user_id"], m["role"]) for m in api_client.get(url, headers=headers).json()
    }
    assert members == {(str(user.id), "PROPIETARIO"), (str(worker.id), "APLICADOR")}

    revoked = api_client.delete(f"{url}/{worker.id}/roles/APLICADOR", headers=headers)
    assert revoked.status_code == 204


def test_the_last_propietario_cannot_be_revoked_over_http(
    api_client: TestClient, owner: tuple[User, Headers]
) -> None:
    user, headers = owner
    predio = _create_predio(api_client, headers)
    response = api_client.delete(
        f"{BASE}/{predio['id']}/users/{user.id}/roles/PROPIETARIO", headers=headers
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {"error": "LastPropietarioError"}


def test_membership_errors_map_to_http(
    api_client: TestClient, headers: Headers, make_client_user: MakeClientUser
) -> None:
    predio = _create_predio(api_client, headers)
    url = f"{BASE}/{predio['id']}/users"
    worker, _ = make_client_user()

    apicultor = api_client.post(
        url, json={"email": worker.email, "role": "APICULTOR"}, headers=headers
    )
    unknown = api_client.post(
        url, json={"email": "nadie@example.cl", "role": "APLICADOR"}, headers=headers
    )
    api_client.post(
        url, json={"email": worker.email, "role": "AGRONOMO"}, headers=headers
    )
    duplicate = api_client.post(
        url, json={"email": worker.email, "role": "AGRONOMO"}, headers=headers
    )

    assert apicultor.status_code == 422
    assert unknown.status_code == 404
    assert duplicate.status_code == 409


def test_an_aplicador_cannot_assign_users(
    api_client: TestClient, headers: Headers, make_client_user: MakeClientUser
) -> None:
    predio = _create_predio(api_client, headers)
    _, aplicador = make_client_user(
        predio_roles=((uuid.UUID(predio["id"]), Role.APLICADOR),)
    )
    other, _ = make_client_user()
    url = f"{BASE}/{predio['id']}/users"

    # An aplicador cannot see the member list either: it needs USER_VIEW.
    assert api_client.get(url, headers=aplicador).status_code == 403
    assert (
        api_client.post(
            url, json={"email": other.email, "role": "APLICADOR"}, headers=aplicador
        ).status_code
        == 403
    )


# --- geometry repair over HTTP ------------------------------------------------

ABOVE_THRESHOLD = g.protruding_hole(
    side_m=1000, hole_height_m=200, inside_m=50, outside_m=100
)
LOTE_ABOVE = g.protruding_hole(side_m=400, hole_height_m=100, inside_m=20, outside_m=20)


def test_a_repair_over_the_threshold_is_a_422_with_the_preview(
    api_client: TestClient, headers: Headers
) -> None:
    response = api_client.post(
        BASE, json={"name": "Reparado", "geometry": ABOVE_THRESHOLD}, headers=headers
    )
    assert response.status_code == 422
    body = response.json()["detail"]
    assert body["error"] == "GeometryRepairExceedsThreshold"
    detail = body["detail"]
    assert detail["repaired_geometry"]["type"] == "Polygon"
    assert detail["area_change_ratio"] == pytest.approx(0.0206, abs=0.001)
    assert detail["threshold_ratio"] == 0.01
    assert detail["message"].startswith("La geometría enviada es inválida.")


def test_the_same_request_with_accept_repair_is_created(
    api_client: TestClient, headers: Headers
) -> None:
    payload = {"name": "Reparado", "geometry": ABOVE_THRESHOLD}
    assert api_client.post(BASE, json=payload, headers=headers).status_code == 422

    response = api_client.post(
        BASE, json={**payload, "accept_repair": True}, headers=headers
    )
    assert response.status_code == 201, response.text
    predio = response.json()["predio"]
    assert predio["geometry_was_repaired"] is True
    assert predio["geometry_repair_area_delta_m2"] == pytest.approx(20_026, rel=0.01)


def test_a_bowtie_is_a_422_with_a_zero_area_reason(
    api_client: TestClient, headers: Headers
) -> None:
    response = api_client.post(
        BASE, json={"name": "Moño", "geometry": g.SELF_INTERSECTING}, headers=headers
    )
    assert response.status_code == 422
    detail = response.json()["detail"]["detail"]
    assert detail["reason"] == "NOT_MEASURABLE_ZERO_AREA"
    assert detail["area_change_ratio"] is None


def test_patch_and_lote_endpoints_take_accept_repair(
    api_client: TestClient, headers: Headers
) -> None:
    predio = _create_predio(api_client, headers)
    url = f"{BASE}/{predio['id']}"

    patched = api_client.patch(url, json={"geometry": ABOVE_THRESHOLD}, headers=headers)
    assert patched.status_code == 422
    patched = api_client.patch(
        url, json={"geometry": ABOVE_THRESHOLD, "accept_repair": True}, headers=headers
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["predio"]["geometry_was_repaired"] is True

    lote = {"name": "Cuartel", "geometry": LOTE_ABOVE}
    assert api_client.post(f"{url}/lotes", json=lote, headers=headers).status_code == 422
    created = api_client.post(
        f"{url}/lotes", json={**lote, "accept_repair": True}, headers=headers
    )
    assert created.status_code == 201, created.text
    lote_id = created.json()["lote"]["id"]
    assert created.json()["lote"]["geometry_was_repaired"] is True

    reset = api_client.patch(
        f"{url}/lotes/{lote_id}",
        json={"geometry": g.square(size=QUARTER)},
        headers=headers,
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["lote"]["geometry_was_repaired"] is False


def test_import_takes_accept_repair_at_the_top_level(
    api_client: TestClient, headers: Headers
) -> None:
    predio = _create_predio(api_client, headers)
    url = f"{BASE}/{predio['id']}/lotes/import-geojson"
    collection = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": LOTE_ABOVE, "properties": {"name": "R"}}
        ],
    }

    rejected = api_client.post(url, json=collection, headers=headers)
    assert rejected.status_code == 422
    [feature] = rejected.json()["detail"]["features"]
    assert feature["error"] == "GeometryRepairExceedsThreshold"

    bad_flag = api_client.post(
        url, json={**collection, "accept_repair": "yes"}, headers=headers
    )
    assert bad_flag.status_code == 422

    accepted = api_client.post(
        url, json={**collection, "accept_repair": True}, headers=headers
    )
    assert accepted.status_code == 201, accepted.text
