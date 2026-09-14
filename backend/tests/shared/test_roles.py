"""Role catalogue invariants."""

from __future__ import annotations

from app.shared.roles import ROLE_LABELS, ROLES_2FA_REQUIRED, Role


def test_system_defines_nine_roles() -> None:
    assert len(Role) == 9


def test_every_role_has_a_label() -> None:
    assert set(ROLE_LABELS) == set(Role)
    assert all(label for label in ROLE_LABELS.values())


def test_two_factor_roles_are_known_roles() -> None:
    assert set(Role) >= ROLES_2FA_REQUIRED
    assert Role.PROPIETARIO in ROLES_2FA_REQUIRED
    assert Role.APLICADOR not in ROLES_2FA_REQUIRED
