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

export const ANALYSIS_TYPES = {
  NDVI: 'NDVI',
} as const

export type AnalysisType = (typeof ANALYSIS_TYPES)[keyof typeof ANALYSIS_TYPES]

export const ANALYSIS_TYPE_LABELS: Record<AnalysisType, string> = {
  NDVI: 'NDVI',
}

export const ANALYSIS_STATUSES = {
  PENDING: 'PENDING',
  RUNNING: 'RUNNING',
  COMPLETED: 'COMPLETED',
  FAILED: 'FAILED',
  NO_SUITABLE_SCENE: 'NO_SUITABLE_SCENE',
  DUPLICATE_SCENE: 'DUPLICATE_SCENE',
} as const

export type AnalysisStatus = (typeof ANALYSIS_STATUSES)[keyof typeof ANALYSIS_STATUSES]

export const ANALYSIS_STATUS_LABELS: Record<AnalysisStatus, string> = {
  PENDING: 'Pendiente',
  RUNNING: 'En proceso',
  COMPLETED: 'Completado',
  FAILED: 'Fallido',
  NO_SUITABLE_SCENE: 'Sin escena utilizable',
  DUPLICATE_SCENE: 'Escena ya analizada',
}

export const ANOMALY_SEVERITIES = {
  LOW: 'LOW',
  MEDIUM: 'MEDIUM',
  HIGH: 'HIGH',
} as const

export type AnomalySeverity = (typeof ANOMALY_SEVERITIES)[keyof typeof ANOMALY_SEVERITIES]

export const ANOMALY_SEVERITY_LABELS: Record<AnomalySeverity, string> = {
  LOW: 'Baja',
  MEDIUM: 'Media',
  HIGH: 'Alta',
}

export const ANOMALY_REVIEW_STATUSES = {
  CONFIRMED: 'CONFIRMED',
  DISMISSED: 'DISMISSED',
  NEEDS_FIELD_CHECK: 'NEEDS_FIELD_CHECK',
} as const

export type AnomalyReviewStatus = (typeof ANOMALY_REVIEW_STATUSES)[keyof typeof ANOMALY_REVIEW_STATUSES]

export const ANOMALY_REVIEW_STATUS_LABELS: Record<AnomalyReviewStatus, string> = {
  CONFIRMED: 'Confirmada',
  DISMISSED: 'Descartada',
  NEEDS_FIELD_CHECK: 'Requiere visita a terreno',
}

export const LOTE_STATS_EXCLUSIONS = {
  NOT_APPLICABLE_GREENHOUSE: 'NOT_APPLICABLE_GREENHOUSE',
  INSUFFICIENT_PIXELS: 'INSUFFICIENT_PIXELS',
} as const

export type LoteStatsExclusion = (typeof LOTE_STATS_EXCLUSIONS)[keyof typeof LOTE_STATS_EXCLUSIONS]

export const LOTE_STATS_EXCLUSION_LABELS: Record<LoteStatsExclusion, string> = {
  NOT_APPLICABLE_GREENHOUSE: 'No aplica: el satélite mide el techo del invernadero, no el cultivo',
  INSUFFICIENT_PIXELS: 'Sin medición: muy pocos píxeles válidos en el lote',
}
