"""Central enum invariants."""

from __future__ import annotations

from enum import StrEnum

import pytest

from app.shared import enums

DOMAIN_ENUMS: list[type[StrEnum]] = [
    enums.DeviceType,
    enums.DeviceAdapterType,
    enums.DeviceStatus,
    enums.OperationType,
    enums.OperationStatus,
    enums.InventoryMovementType,
    enums.AuditEventCategory,
    enums.AuditEventSeverity,
    enums.AuditOutcome,
]


@pytest.mark.parametrize("enum_type", DOMAIN_ENUMS)
def test_domain_enum_values_match_member_names(enum_type: type[StrEnum]) -> None:
    assert all(member.value == member.name for member in enum_type)


def test_environment_values_are_lowercase() -> None:
    assert [member.value for member in enums.Environment] == [
        "development",
        "staging",
        "production",
    ]
