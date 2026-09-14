"""JWT helper behaviour."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.shared.security import TokenError, create_access_token, decode_access_token


def test_token_roundtrip_preserves_subject_and_claims() -> None:
    token = create_access_token("user-1", claims={"role": "AUDITOR"})
    payload = decode_access_token(token)
    assert payload["sub"] == "user-1"
    assert payload["role"] == "AUDITOR"
    assert payload["exp"] > payload["iat"]


def test_expired_token_is_rejected() -> None:
    token = create_access_token("user-1", expires_delta=timedelta(seconds=-1))
    with pytest.raises(TokenError):
        decode_access_token(token)


def test_tampered_token_is_rejected() -> None:
    token = create_access_token("user-1")
    with pytest.raises(TokenError):
        decode_access_token(token + "tampered")
