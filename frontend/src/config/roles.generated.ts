// AUTO-GENERATED — DO NOT EDIT BY HAND.
// Source: backend/app/shared/roles.py
// Regenerate: python scripts/generate_frontend_roles.py

export const ROLES = {
  PROPIETARIO: 'PROPIETARIO',
  ADMIN_OPERACIONES: 'ADMIN_OPERACIONES',
  AGRONOMO: 'AGRONOMO',
  OPERADOR_DRONE: 'OPERADOR_DRONE',
  APLICADOR: 'APLICADOR',
  BODEGUERO: 'BODEGUERO',
  JEFE_BODEGA: 'JEFE_BODEGA',
  AUDITOR: 'AUDITOR',
  APICULTOR: 'APICULTOR',
} as const

export type Role = (typeof ROLES)[keyof typeof ROLES]

export interface RoleMeta {
  label: string
  shortLabel: string
  color: string
  description: string
}

export const ROLE_META: Record<Role, RoleMeta> = {
  PROPIETARIO: {
    label: 'Propietario',
    shortLabel: 'Propietario',
    color: 'blue-700',
    description: 'Dueño del predio. Ve todo lo suyo, asigna usuarios y configura políticas.',
  },
  ADMIN_OPERACIONES: {
    label: 'Administrador de Operaciones',
    shortLabel: 'Admin Operaciones',
    color: 'green-600',
    description: 'Planifica misiones, asigna tareas y aprueba aplicaciones.',
  },
  AGRONOMO: {
    label: 'Agrónomo',
    shortLabel: 'Agrónomo',
    color: 'emerald-700',
    description: 'Analiza datos, emite reportes técnicos y recomienda tratamientos.',
  },
  OPERADOR_DRONE: {
    label: 'Operador de Drone',
    shortLabel: 'Operador',
    color: 'orange-500',
    description: 'Ejecuta misiones de vuelo y registra capturas.',
  },
  APLICADOR: {
    label: 'Aplicador',
    shortLabel: 'Aplicador',
    color: 'amber-600',
    description: 'Ejecuta aplicaciones de productos y declara EPP utilizado.',
  },
  BODEGUERO: {
    label: 'Bodeguero',
    shortLabel: 'Bodeguero',
    color: 'stone-600',
    description: 'Registra entradas y salidas normales de bodega.',
  },
  JEFE_BODEGA: {
    label: 'Jefe de Bodega',
    shortLabel: 'Jefe Bodega',
    color: 'stone-800',
    description: 'Aprueba movimientos restringidos y gestiona el inventario.',
  },
  AUDITOR: {
    label: 'Auditor',
    shortLabel: 'Auditor',
    color: 'purple-700',
    description: 'Acceso de solo lectura a todo el sistema. Exporta trazabilidad.',
  },
  APICULTOR: {
    label: 'Apicultor Registrado',
    shortLabel: 'Apicultor',
    color: 'yellow-600',
    description: 'Externo. Recibe avisos de aplicaciones cercanas a sus colmenas.',
  },
}

export const ROLES_2FA_REQUIRED: ReadonlySet<Role> = new Set<Role>([
  ROLES.PROPIETARIO,
  ROLES.ADMIN_OPERACIONES,
  ROLES.AGRONOMO,
  ROLES.JEFE_BODEGA,
  ROLES.AUDITOR,
])
