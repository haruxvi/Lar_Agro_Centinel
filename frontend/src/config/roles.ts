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

export const ROLE_LABELS: Record<Role, string> = {
  PROPIETARIO: 'Propietario',
  ADMIN_OPERACIONES: 'Admin Operaciones',
  AGRONOMO: 'Agrónomo',
  OPERADOR_DRONE: 'Operador de Drone',
  APLICADOR: 'Aplicador',
  BODEGUERO: 'Bodeguero',
  JEFE_BODEGA: 'Jefe de Bodega',
  AUDITOR: 'Auditor',
  APICULTOR: 'Apicultor Registrado',
}
