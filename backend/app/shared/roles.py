"""System roles.

This module is the single source of truth for the role catalogue. The frontend
constants in ``frontend/src/config/roles.generated.ts`` are generated from it by
``scripts/generate_frontend_roles.py``; CI fails when both drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    """Role assigned to a user, globally or scoped to a predio."""

    PROPIETARIO = "PROPIETARIO"
    ADMIN_OPERACIONES = "ADMIN_OPERACIONES"
    AGRONOMO = "AGRONOMO"
    OPERADOR_DRONE = "OPERADOR_DRONE"
    APLICADOR = "APLICADOR"
    BODEGUERO = "BODEGUERO"
    JEFE_BODEGA = "JEFE_BODEGA"
    AUDITOR = "AUDITOR"
    APICULTOR = "APICULTOR"


@dataclass(frozen=True)
class RoleMeta:
    """Presentation metadata for a role."""

    label: str
    """Full name, for documents, reports and administration screens."""

    short_label: str
    """Short form, for badges, sidebars and dense tables."""

    color: str
    """Tailwind color token."""

    description: str
    """One-line description, for tooltips and documentation."""


ROLE_META: dict[Role, RoleMeta] = {
    Role.PROPIETARIO: RoleMeta(
        label="Propietario",
        short_label="Propietario",
        color="blue-700",
        description=(
            "Dueño del predio. Ve todo lo suyo, asigna usuarios y configura políticas."
        ),
    ),
    Role.ADMIN_OPERACIONES: RoleMeta(
        label="Administrador de Operaciones",
        short_label="Admin Operaciones",
        color="green-600",
        description="Planifica misiones, asigna tareas y aprueba aplicaciones.",
    ),
    Role.AGRONOMO: RoleMeta(
        label="Agrónomo",
        short_label="Agrónomo",
        color="emerald-700",
        description="Analiza datos, emite reportes técnicos y recomienda tratamientos.",
    ),
    Role.OPERADOR_DRONE: RoleMeta(
        label="Operador de Drone",
        short_label="Operador",
        color="orange-500",
        description="Ejecuta misiones de vuelo y registra capturas.",
    ),
    Role.APLICADOR: RoleMeta(
        label="Aplicador",
        short_label="Aplicador",
        color="amber-600",
        description="Ejecuta aplicaciones de productos y declara EPP utilizado.",
    ),
    Role.BODEGUERO: RoleMeta(
        label="Bodeguero",
        short_label="Bodeguero",
        color="stone-600",
        description="Registra entradas y salidas normales de bodega.",
    ),
    Role.JEFE_BODEGA: RoleMeta(
        label="Jefe de Bodega",
        short_label="Jefe Bodega",
        color="stone-800",
        description="Aprueba movimientos restringidos y gestiona el inventario.",
    ),
    Role.AUDITOR: RoleMeta(
        label="Auditor",
        short_label="Auditor",
        color="purple-700",
        description="Acceso de solo lectura a todo el sistema. Exporta trazabilidad.",
    ),
    Role.APICULTOR: RoleMeta(
        label="Apicultor Registrado",
        short_label="Apicultor",
        color="yellow-600",
        description="Externo. Recibe avisos de aplicaciones cercanas a sus colmenas.",
    ),
}

# Immutable on purpose: a security policy must not be altered at runtime.
ROLES_2FA_REQUIRED: frozenset[Role] = frozenset(
    {
        Role.PROPIETARIO,
        Role.ADMIN_OPERACIONES,
        Role.JEFE_BODEGA,
        Role.AUDITOR,
        Role.AGRONOMO,
    }
)
