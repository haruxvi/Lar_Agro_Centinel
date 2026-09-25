// AUTO-GENERATED — DO NOT EDIT BY HAND.
// Source: backend/app/shared/enums.py
// Regenerate: python scripts/generate_frontend_enums.py

export const LOTE_TYPES = {
  CUARTEL: 'CUARTEL',
  POTRERO: 'POTRERO',
  PARCELA: 'PARCELA',
  INVERNADERO: 'INVERNADERO',
  BODEGA_AREA: 'BODEGA_AREA',
  OTRO: 'OTRO',
} as const

export type LoteType = (typeof LOTE_TYPES)[keyof typeof LOTE_TYPES]

export const LOTE_TYPE_LABELS: Record<LoteType, string> = {
  CUARTEL: 'Cuartel',
  POTRERO: 'Potrero',
  PARCELA: 'Parcela',
  INVERNADERO: 'Invernadero',
  BODEGA_AREA: 'Área de bodega',
  OTRO: 'Otro',
}
