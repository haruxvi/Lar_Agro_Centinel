"""Central enumerations shared across bounded contexts."""

from __future__ import annotations

from enum import StrEnum


class DeviceType(StrEnum):
    """Kind of physical device registered in a predio."""

    DRONE = "DRONE"
    TRACTOR = "TRACTOR"
    SENSOR = "SENSOR"
    TRACKER = "TRACKER"


class DeviceAdapterType(StrEnum):
    """Integration used to exchange data with a device."""

    MANUAL_UPLOAD = "MANUAL_UPLOAD"
    DJI_SDK = "DJI_SDK"
    MAVLINK = "MAVLINK"
    LORA_GPS = "LORA_GPS"


class DeviceStatus(StrEnum):
    """Lifecycle status of a device."""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    MAINTENANCE = "MAINTENANCE"
    RETIRED = "RETIRED"


class OperationType(StrEnum):
    """Kind of field operation."""

    DRONE_MISSION = "DRONE_MISSION"
    PRODUCT_APPLICATION = "PRODUCT_APPLICATION"
    INSPECTION = "INSPECTION"
    MAINTENANCE = "MAINTENANCE"


class OperationStatus(StrEnum):
    """Lifecycle status of a field operation."""

    PLANNED = "PLANNED"
    APPROVED = "APPROVED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class InventoryMovementType(StrEnum):
    """Direction or nature of an inventory movement."""

    IN = "IN"
    OUT = "OUT"
    ADJUSTMENT = "ADJUSTMENT"
    LOSS = "LOSS"


class AuditEventCategory(StrEnum):
    """Broad category of an audit event."""

    SECURITY = "SECURITY"
    DOMAIN = "DOMAIN"
    SYSTEM = "SYSTEM"


class AuditEventSeverity(StrEnum):
    """Severity of an audit event."""

    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class AuditOutcome(StrEnum):
    """Result of the action recorded by an audit event."""

    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    DENIED = "DENIED"


class Environment(StrEnum):
    """Deployment environment the application runs in."""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class LoteType(StrEnum):
    """Kind of subdivision inside a predio."""

    CUARTEL = "CUARTEL"  # vineyard or orchard block
    POTRERO = "POTRERO"  # grazing paddock
    PARCELA = "PARCELA"  # annual crop plot
    INVERNADERO = "INVERNADERO"
    BODEGA_AREA = "BODEGA_AREA"  # infrastructure zone
    OTRO = "OTRO"


LOTE_TYPE_LABELS: dict[LoteType, str] = {
    LoteType.CUARTEL: "Cuartel",
    LoteType.POTRERO: "Potrero",
    LoteType.PARCELA: "Parcela",
    LoteType.INVERNADERO: "Invernadero",
    LoteType.BODEGA_AREA: "Área de bodega",
    LoteType.OTRO: "Otro",
}


# --- Satellite analysis (Phase 3) ------------------------------------------------


class AnalysisType(StrEnum):
    """Index computed by an analysis. NDVI only, until others exist."""

    NDVI = "NDVI"


ANALYSIS_TYPE_LABELS: dict[AnalysisType, str] = {
    AnalysisType.NDVI: "NDVI",
}


class AnalysisStatus(StrEnum):
    """Lifecycle of an analysis.

    NO_SUITABLE_SCENE is terminal and distinct from FAILED on purpose: a
    cloudy sky is a condition of the world, not a system error, and mixing
    them would hide real failures behind a run of bad weather.
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    NO_SUITABLE_SCENE = "NO_SUITABLE_SCENE"
    # The scene already had a completed analysis: not an error, the result
    # exists. resolved_to_analysis_id points at it.
    DUPLICATE_SCENE = "DUPLICATE_SCENE"


ANALYSIS_STATUS_LABELS: dict[AnalysisStatus, str] = {
    AnalysisStatus.PENDING: "Pendiente",
    AnalysisStatus.RUNNING: "En proceso",
    AnalysisStatus.COMPLETED: "Completado",
    AnalysisStatus.FAILED: "Fallido",
    AnalysisStatus.NO_SUITABLE_SCENE: "Sin escena utilizable",
    AnalysisStatus.DUPLICATE_SCENE: "Escena ya analizada",
}


class AnomalySeverity(StrEnum):
    """How strongly an anomaly stands out within its lote (provisional cuts)."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


ANOMALY_SEVERITY_LABELS: dict[AnomalySeverity, str] = {
    AnomalySeverity.LOW: "Baja",
    AnomalySeverity.MEDIUM: "Media",
    AnomalySeverity.HIGH: "Alta",
}


class AnomalyReviewStatus(StrEnum):
    """An agronomist's verdict on an anomaly, which is a hypothesis until then."""

    CONFIRMED = "CONFIRMED"
    DISMISSED = "DISMISSED"
    NEEDS_FIELD_CHECK = "NEEDS_FIELD_CHECK"


ANOMALY_REVIEW_STATUS_LABELS: dict[AnomalyReviewStatus, str] = {
    AnomalyReviewStatus.CONFIRMED: "Confirmada",
    AnomalyReviewStatus.DISMISSED: "Descartada",
    AnomalyReviewStatus.NEEDS_FIELD_CHECK: "Requiere visita a terreno",
}


class LoteStatsExclusion(StrEnum):
    """Why a lote has counts but no statistics in an analysis."""

    NOT_APPLICABLE_GREENHOUSE = "NOT_APPLICABLE_GREENHOUSE"
    INSUFFICIENT_PIXELS = "INSUFFICIENT_PIXELS"


LOTE_STATS_EXCLUSION_LABELS: dict[LoteStatsExclusion, str] = {
    LoteStatsExclusion.NOT_APPLICABLE_GREENHOUSE: (
        "No aplica: el satélite mide el techo del invernadero, no el cultivo"
    ),
    LoteStatsExclusion.INSUFFICIENT_PIXELS: (
        "Sin medición: muy pocos píxeles válidos en el lote"
    ),
}
