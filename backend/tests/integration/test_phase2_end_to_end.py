"""Phase 2 end to end, through HTTP only, with real sessions.

One scenario, in the order a real owner would go through it: log in with a
second factor, create a predio, preview a repair, add lotes by hand and by
import, bring in a worker, check that a stranger sees nothing, revoke the
worker, and dismantle it all. Nothing is set up behind the API's back except
what the API cannot do yet: the owner's global PROPIETARIO grant and 2FA
secret.
"""

from __future__ import annotations

import json
import uuid

import pyotp
import redis
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit import events
from app.modules.audit.models import AuditLog
from app.modules.auth import security as passwords
from app.modules.auth import two_factor
from app.modules.auth.models import UserTwoFactor
from app.modules.users.models import User, UserRole
from app.shared.geo import contains_geojson
from app.shared.roles import Role
from tests.fixtures import geometries as g

AUTH = "/api/v1/auth"
PREDIOS = "/api/v1/predios"
PASSWORD = "Cordillera-Sur-2026"  # noqa: S105 - test fixture, not a credential
QUARTER = g.STEP / 4
HALF = g.STEP / 2


def _owner_with_two_factor(session: Session) -> tuple[str, str]:
    """Create a PROPIETARIO with 2FA enabled; return its email and TOTP secret."""
    email = f"owner-{uuid.uuid4().hex[:8]}@example.cl"
    user = User(
        email=email, password_hash=passwords.hash_password(PASSWORD), full_name="Dueña"
    )
    session.add(user)
    session.flush()
    session.add(UserRole(user_id=user.id, role=Role.PROPIETARIO))
    secret = two_factor.generate_secret()
    session.add(
        UserTwoFactor(
            user_id=user.id,
            totp_secret_encrypted=two_factor.encrypt_secret(secret),
            totp_verified=True,
            enabled=True,
            recovery_codes_hash=[],
            failed_attempts=0,
        )
    )
    session.flush()
    return email, secret


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register_and_login(client: TestClient, name: str) -> tuple[str, dict[str, str]]:
    email = f"{name}-{uuid.uuid4().hex[:8]}@example.cl"
    registered = client.post(
        f"{AUTH}/register",
        json={"email": email, "password": PASSWORD, "full_name": name.title()},
    )
    assert registered.status_code == 201, registered.text
    login = client.post(f"{AUTH}/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return email, _bearer(login.json()["access_token"])


def _feature(geometry: dict[str, object], name: str, lote_type: str) -> dict[str, object]:
    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": {"name": name, "lote_type": lote_type},
    }


def test_an_owner_runs_a_predio_from_creation_to_removal(
    api_client: TestClient, app_session: Session, redis_client: redis.Redis
) -> None:
    client = api_client

    # --- the owner logs in with a second factor -------------------------------
    owner_email, secret = _owner_with_two_factor(app_session)
    challenge = client.post(
        f"{AUTH}/login", json={"email": owner_email, "password": PASSWORD}
    ).json()["challenge_token"]
    verified = client.post(
        f"{AUTH}/2fa/verify",
        json={"challenge_token": challenge, "code": pyotp.TOTP(secret).now()},
    )
    assert verified.status_code == 200, verified.text
    owner = _bearer(verified.json()["access_token"])

    # --- previewing a boundary that would need confirmation --------------------
    suspicious = g.protruding_hole(
        side_m=1000, hole_height_m=200, inside_m=50, outside_m=100
    )
    preview = client.post(
        f"{PREDIOS}/validate-geometry", json={"geometry": suspicious}, headers=owner
    ).json()
    assert preview["repair"]["requires_confirmation"] is True

    # --- creating the predio with a clean boundary ----------------------------
    created = client.post(
        PREDIOS,
        json={"name": "Viña Los Aromos", "comuna": "Santa Cruz", "geometry": g.SQUARE},
        headers=owner,
    )
    assert created.status_code == 201, created.text
    predio_id = created.json()["predio"]["id"]
    predio_url = f"{PREDIOS}/{predio_id}"

    # --- lotes, one by hand and two by import ----------------------------------
    by_hand = client.post(
        f"{predio_url}/lotes",
        json={
            "name": "Cuartel 1",
            "lote_type": "CUARTEL",
            "variety": "Carménère",
            "geometry": g.square(size=QUARTER),
        },
        headers=owner,
    )
    assert by_hand.status_code == 201, by_hand.text
    imported = client.post(
        f"{predio_url}/lotes/import-geojson",
        json={
            "type": "FeatureCollection",
            "features": [
                _feature(
                    g.square(lon=g.BASE_LON + HALF, size=QUARTER), "Cuartel 2", "CUARTEL"
                ),
                _feature(
                    g.square(lat=g.BASE_LAT + HALF, size=QUARTER), "Potrero", "POTRERO"
                ),
            ],
        },
        headers=owner,
    )
    assert imported.status_code == 201, imported.text

    summary = client.get(f"{predio_url}/summary", headers=owner).json()
    assert summary["lotes_count"] == 3
    assert summary["lotes_by_type"] == {"CUARTEL": 2, "POTRERO": 1}

    # --- a worker is brought in, and sees only what the role allows ------------
    worker_email, worker = _register_and_login(client, "aplicador")
    assigned = client.post(
        f"{predio_url}/users",
        json={"email": worker_email, "role": "APLICADOR"},
        headers=owner,
    )
    assert assigned.status_code == 201, assigned.text
    worker_id = assigned.json()["user_id"]

    assert client.get(predio_url, headers=worker).status_code == 200
    listed = client.get(PREDIOS, headers=worker).json()["items"]
    assert [item["id"] for item in listed] == [predio_id]
    assert (
        client.patch(predio_url, json={"name": "Mía"}, headers=worker).status_code == 403
    )

    # --- a stranger sees nothing ------------------------------------------------
    _, stranger = _register_and_login(client, "extrano")
    assert client.get(predio_url, headers=stranger).status_code == 403
    assert client.get(PREDIOS, headers=stranger).json()["total"] == 0

    # --- the worker is revoked; the token they hold no longer opens the door --
    revoked = client.delete(
        f"{predio_url}/users/{worker_id}/roles/APLICADOR", headers=owner
    )
    assert revoked.status_code == 204
    assert client.get(predio_url, headers=worker).status_code == 403

    # --- the owner cannot leave the predio ownerless ----------------------------
    owner_id = created.json()["predio"]["owner_user_id"]
    lonely = client.delete(
        f"{predio_url}/users/{owner_id}/roles/PROPIETARIO", headers=owner
    )
    assert lonely.status_code == 409

    # --- dismantling: lotes first, then the predio ------------------------------
    assert client.delete(predio_url, headers=owner).status_code == 409
    for lote in client.get(f"{predio_url}/lotes", headers=owner).json():
        deleted = client.delete(f"{predio_url}/lotes/{lote['id']}", headers=owner)
        assert deleted.status_code == 204
    assert client.delete(predio_url, headers=owner).status_code == 204
    assert client.get(predio_url, headers=owner).status_code == 404

    # --- the audit trail tells the whole story, without a single geometry ------
    trail = list(
        app_session.execute(
            select(AuditLog).where(AuditLog.predio_id == uuid.UUID(predio_id))
        ).scalars()
    )
    # No order is asserted: audit timestamps are the transaction's start time
    # (now()), and this whole test runs in one transaction. See KL-003.
    recorded = [entry.event_type for entry in trail]
    for expected in (
        events.PREDIO_CREATED,
        events.LOTE_CREATED,
        events.LOTES_IMPORTED,
        events.PREDIO_USER_ASSIGNED,
        events.AUTHORIZATION_DENIED,
        events.PREDIO_USER_UNASSIGNED,
        events.LOTE_DELETED,
        events.PREDIO_DELETED,
    ):
        assert expected in recorded, expected
    for entry in trail:
        assert not contains_geojson(entry.details), entry.event_type
        assert "coordinates" not in json.dumps(entry.details)
