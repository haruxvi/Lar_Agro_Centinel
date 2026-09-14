"""System roles.

This enum is the single source of truth on the backend and must stay in sync
with ``frontend-v2/src/config/roles.ts``.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """Role assigned to a user inside a tenant."""

    PROPIETARIO = "PROPIETARIO"
    ADMIN_OPERACIONES = "ADMIN_OPERACIONES"
    AGRONOMO = "AGRONOMO"
    OPERADOR_DRONE = "OPERADOR_DRONE"
    APLICADOR = "APLICADOR"
    BODEGUERO = "BODEGUERO"
    JEFE_BODEGA = "JEFE_BODEGA"
    AUDITOR = "AUDITOR"
    APICULTOR = "APICULTOR"


ROLE_LABELS: dict[Role, str] = {
    Role.PROPIETARIO: "Propietario",
    Role.ADMIN_OPERACIONES: "Admin Operaciones",
    Role.AGRONOMO: "Agrónomo",
    Role.OPERADOR_DRONE: "Operador de Drone",
    Role.APLICADOR: "Aplicador",
    Role.BODEGUERO: "Bodeguero",
    Role.JEFE_BODEGA: "Jefe de Bodega",
    Role.AUDITOR: "Auditor",
    Role.APICULTOR: "Apicultor Registrado",
}

ROLES_2FA_REQUIRED: frozenset[Role] = frozenset(
    {
        Role.PROPIETARIO,
        Role.ADMIN_OPERACIONES,
        Role.JEFE_BODEGA,
        Role.AUDITOR,
        Role.AGRONOMO,
    }
)
