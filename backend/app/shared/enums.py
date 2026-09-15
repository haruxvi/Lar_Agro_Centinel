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
