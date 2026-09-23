"""Role catalogue invariants."""

from __future__ import annotations

from app.shared.roles import ROLE_META, ROLES_2FA_REQUIRED, Role


def test_system_defines_nine_roles() -> None:
    assert len(Role) == 9


def test_every_role_has_metadata() -> None:
    assert set(ROLE_META) == set(Role)


def test_metadata_fields_are_populated() -> None:
    for role, meta in ROLE_META.items():
        assert meta.label, role
        assert meta.short_label, role
        assert meta.color, role
        assert meta.description, role


def test_short_label_is_not_longer_than_label() -> None:
    for role, meta in ROLE_META.items():
        assert len(meta.short_label) <= len(meta.label), role


def test_labels_are_unique() -> None:
    labels = [meta.label for meta in ROLE_META.values()]
    assert len(set(labels)) == len(labels)


def test_role_metadata_is_immutable() -> None:
    import dataclasses

    import pytest

    meta = ROLE_META[Role.AUDITOR]
    with pytest.raises(dataclasses.FrozenInstanceError):
        meta.label = "tampered"  # type: ignore[misc]


def test_two_factor_roles_are_known_roles() -> None:
    assert set(Role) >= ROLES_2FA_REQUIRED
    assert Role.PROPIETARIO in ROLES_2FA_REQUIRED
    assert Role.APLICADOR not in ROLES_2FA_REQUIRED
