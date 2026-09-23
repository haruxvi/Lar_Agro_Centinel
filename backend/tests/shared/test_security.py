"""JWT issuing and fail-closed verification."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest

from app.shared.config import get_settings
from app.shared.roles import Role
from app.shared.security import (
    AUDIENCE,
    PURPOSE_TWO_FACTOR,
    InvalidTokenError,
    create_access_token,
    create_challenge_token,
    create_refresh_token,
    decode_token,
    hash_refresh_token,
)

USER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
PREDIO_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _secret() -> str:
    return get_settings().secret_key.get_secret_value()


def _claims(**overrides: Any) -> dict[str, Any]:
    settings = get_settings()
    now = int(datetime.now(UTC).timestamp())
    claims: dict[str, Any] = {
        "sub": str(USER_ID),
        "iss": settings.jwt_issuer,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 600,
        "jti": str(uuid.uuid4()),
        "roles": [Role.AGRONOMO.value],
        "active_role": Role.AGRONOMO.value,
        "predio_ids": [str(PREDIO_ID)],
        "2fa_verified": True,
        "purpose": "access",
    }
    claims.update(overrides)
    return claims


def _encode(
    claims: dict[str, Any], *, key: str | None = None, algorithm: str = "HS256"
) -> str:
    return jwt.encode(claims, key if key is not None else _secret(), algorithm=algorithm)


# --- issuing -----------------------------------------------------------------


def test_access_token_roundtrip() -> None:
    token = create_access_token(
        USER_ID,
        roles=[Role.AGRONOMO, Role.AUDITOR],
        active_role=Role.AUDITOR,
        predio_ids=[PREDIO_ID],
        two_factor_verified=True,
    )
    payload = decode_token(token)

    assert payload.user_id == USER_ID
    assert payload.roles == [Role.AGRONOMO, Role.AUDITOR]
    assert payload.active_role is Role.AUDITOR
    assert payload.predio_ids == [PREDIO_ID]
    assert payload.two_factor_verified is True
    assert payload.aud == AUDIENCE
    assert payload.iss == get_settings().jwt_issuer
    assert payload.exp > payload.iat


def test_every_access_token_has_a_unique_jti() -> None:
    first = decode_token(create_access_token(USER_ID, [Role.AUDITOR], Role.AUDITOR))
    second = decode_token(create_access_token(USER_ID, [Role.AUDITOR], Role.AUDITOR))
    assert first.jti != second.jti


def test_challenge_token_carries_no_session_authority() -> None:
    payload = decode_token(create_challenge_token(USER_ID, timedelta(minutes=5)))
    assert payload.purpose == PURPOSE_TWO_FACTOR
    assert payload.roles == []
    assert payload.active_role is None
    assert payload.two_factor_verified is False


def test_refresh_token_is_returned_with_its_hash_only() -> None:
    plain, hashed = create_refresh_token(USER_ID)
    assert plain != hashed
    assert hashed == hash_refresh_token(plain)
    assert len(hashed) == 64  # SHA-256 hex digest
    assert plain not in hashed


def test_refresh_tokens_are_unpredictable() -> None:
    tokens = {create_refresh_token(USER_ID)[0] for _ in range(10)}
    assert len(tokens) == 10


# --- verification: each of these must fail closed -----------------------------


def test_tampered_signature_is_rejected() -> None:
    token = create_access_token(USER_ID, [Role.AUDITOR], Role.AUDITOR)
    with pytest.raises(InvalidTokenError):
        decode_token(token + "x")


def test_token_signed_with_another_key_is_rejected() -> None:
    token = _encode(_claims(), key="a-different-key-of-at-least-32-characters")
    with pytest.raises(InvalidTokenError):
        decode_token(token)


def test_algorithm_none_is_rejected() -> None:
    token = jwt.encode(_claims(), key=None, algorithm="none")  # type: ignore[arg-type]
    with pytest.raises(InvalidTokenError):
        decode_token(token)


def test_unexpected_algorithm_is_rejected() -> None:
    # Same secret, different algorithm: the header's `alg` must not be trusted.
    token = _encode(_claims(), algorithm="HS512")
    with pytest.raises(InvalidTokenError):
        decode_token(token)


def test_expired_token_is_rejected() -> None:
    leeway = get_settings().jwt_leeway_seconds
    now = int(datetime.now(UTC).timestamp())
    token = _encode(_claims(iat=now - 3600, exp=now - leeway - 60))
    with pytest.raises(InvalidTokenError):
        decode_token(token)


def test_wrong_issuer_is_rejected() -> None:
    with pytest.raises(InvalidTokenError):
        decode_token(_encode(_claims(iss="someone-else")))


def test_wrong_audience_is_rejected() -> None:
    with pytest.raises(InvalidTokenError):
        decode_token(_encode(_claims(aud="another-api")))


def test_future_issued_at_is_rejected() -> None:
    settings = get_settings()
    future = int(datetime.now(UTC).timestamp()) + settings.jwt_leeway_seconds + 120
    # PyJWT rejects a future `iat` itself; decode_token keeps its own check as a
    # backstop in case that behaviour changes. Either message is acceptable.
    with pytest.raises(InvalidTokenError, match="(?i)(future|not yet valid)"):
        decode_token(_encode(_claims(iat=future, exp=future + 600)))


def test_unknown_role_is_rejected() -> None:
    with pytest.raises(InvalidTokenError):
        decode_token(_encode(_claims(roles=["SUPERADMIN"], active_role="SUPERADMIN")))


def test_active_role_outside_granted_roles_is_rejected() -> None:
    claims = _claims(roles=[Role.APLICADOR.value], active_role=Role.PROPIETARIO.value)
    with pytest.raises(InvalidTokenError, match="active_role"):
        decode_token(_encode(claims))


def test_access_token_without_active_role_is_rejected() -> None:
    claims = _claims()
    del claims["active_role"]
    with pytest.raises(InvalidTokenError, match="active_role"):
        decode_token(_encode(claims))


@pytest.mark.parametrize("claim", ["sub", "iss", "aud", "iat", "exp", "jti"])
def test_missing_required_claim_is_rejected(claim: str) -> None:
    claims = _claims()
    del claims[claim]
    with pytest.raises(InvalidTokenError):
        decode_token(_encode(claims))


def test_non_uuid_subject_is_rejected() -> None:
    with pytest.raises(InvalidTokenError):
        decode_token(_encode(_claims(sub="not-a-uuid")))


def test_non_uuid_predio_id_is_rejected() -> None:
    with pytest.raises(InvalidTokenError):
        decode_token(_encode(_claims(predio_ids=["not-a-uuid"])))


@pytest.mark.parametrize(
    "malformed",
    ["", "abc", "a.b.c", "Bearer token", "...", "eyJhbGciOiJIUzI1NiJ9"],
)
def test_malformed_token_is_rejected(malformed: str) -> None:
    with pytest.raises(InvalidTokenError):
        decode_token(malformed)


def test_error_message_does_not_leak_the_secret() -> None:
    try:
        decode_token("not-a-token")
    except InvalidTokenError as exc:
        assert _secret() not in str(exc)
    else:  # pragma: no cover - the call above always raises
        pytest.fail("decode_token accepted a malformed token")


def test_a_user_without_roles_gets_a_valid_token() -> None:
    # A freshly registered user holds no roles until someone grants them.
    payload = decode_token(create_access_token(USER_ID, roles=[], active_role=None))
    assert payload.roles == []
    assert payload.active_role is None
