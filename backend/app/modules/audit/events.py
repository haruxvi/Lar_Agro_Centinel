"""Audit event types recorded by the platform.

Constants rather than free-form strings: the event type is what queries and
retention rules key on, so a typo would quietly lose a record.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from app.shared.enums import AuditEventCategory, AuditEventSeverity

# Accounts
USER_REGISTERED: Final = "USER_REGISTERED"
PASSWORD_CHANGED: Final = "PASSWORD_CHANGED"  # noqa: S105 - event name, not a secret
PASSWORD_REHASHED: Final = "PASSWORD_REHASHED"  # noqa: S105 - event name, not a secret
ACCOUNT_LOCKED: Final = "ACCOUNT_LOCKED"

# Sessions
LOGIN_SUCCESS: Final = "LOGIN_SUCCESS"
LOGIN_FAILED: Final = "LOGIN_FAILED"
LOGIN_2FA_REQUIRED: Final = "LOGIN_2FA_REQUIRED"
LOGOUT: Final = "LOGOUT"
TOKEN_REFRESHED: Final = "TOKEN_REFRESHED"  # noqa: S105 - event name, not a secret
REFRESH_TOKEN_REUSE_DETECTED: Final = "REFRESH_TOKEN_REUSE_DETECTED"  # noqa: S105

# Two-factor authentication
TWO_FACTOR_ENROLLMENT_STARTED: Final = "2FA_ENROLLMENT_STARTED"
TWO_FACTOR_ENABLED: Final = "2FA_ENABLED"
TWO_FACTOR_DISABLED: Final = "2FA_DISABLED"
TWO_FACTOR_VERIFIED: Final = "2FA_VERIFIED"
TWO_FACTOR_FAILED: Final = "2FA_FAILED"
TWO_FACTOR_LOCKED: Final = "2FA_LOCKED"
TWO_FACTOR_RECOVERY_CODE_USED: Final = "2FA_RECOVERY_CODE_USED"

# Authorization
AUTHORIZATION_DENIED: Final = "AUTHORIZATION_DENIED"
ROLE_GRANTED: Final = "ROLE_GRANTED"
ROLE_REVOKED: Final = "ROLE_REVOKED"
ROLE_SWITCHED: Final = "ROLE_SWITCHED"

# Predios. Details carry areas, bounding boxes and vertex counts, never a full
# geometry: the audit log is not a geometry store (see KL-001).
PREDIO_CREATED: Final = "PREDIO_CREATED"
PREDIO_UPDATED: Final = "PREDIO_UPDATED"
PREDIO_GEOMETRY_CHANGED: Final = "PREDIO_GEOMETRY_CHANGED"
PREDIO_DELETED: Final = "PREDIO_DELETED"
PREDIO_USER_ASSIGNED: Final = "PREDIO_USER_ASSIGNED"
PREDIO_USER_UNASSIGNED: Final = "PREDIO_USER_UNASSIGNED"

# Lotes
LOTE_CREATED: Final = "LOTE_CREATED"
LOTE_UPDATED: Final = "LOTE_UPDATED"
LOTE_GEOMETRY_CHANGED: Final = "LOTE_GEOMETRY_CHANGED"
LOTE_DELETED: Final = "LOTE_DELETED"
LOTES_IMPORTED: Final = "LOTES_IMPORTED"

# Automatic geometry repairs (predios and lotes). Details carry areas, delta,
# ratio, threshold and bbox; never the geometry, original or repaired.
GEOMETRY_REPAIRED: Final = "GEOMETRY_REPAIRED"
GEOMETRY_REPAIR_ACCEPTED: Final = "GEOMETRY_REPAIR_ACCEPTED"
GEOMETRY_REPAIR_REJECTED: Final = "GEOMETRY_REPAIR_REJECTED"

# Sentinel Hub (Phase 3). Catalogued with the rest of the analysis events.
SENTINEL_QUOTA_EXCEEDED: Final = "SENTINEL_QUOTA_EXCEEDED"
SENTINEL_CIRCUIT_OPEN: Final = "SENTINEL_CIRCUIT_OPEN"


@dataclass(frozen=True)
class EventSpec:
    """How an event type is recorded: declared once, not decided per call."""

    category: AuditEventCategory
    severity: AuditEventSeverity


_INFO = AuditEventSeverity.INFO
_WARNING = AuditEventSeverity.WARNING
_DOMAIN = AuditEventCategory.DOMAIN
_SECURITY = AuditEventCategory.SECURITY

# Catalogue of the predios context, documented in docs/audit-events.md (a test
# keeps both in sync). WARNING marks what cannot be undone from the audit
# trail alone: a boundary overwritten (KL-001), a deletion, a change of who
# may act on a predio.
PREDIO_EVENTS: Final[Mapping[str, EventSpec]] = MappingProxyType(
    {
        PREDIO_CREATED: EventSpec(_DOMAIN, _INFO),
        PREDIO_UPDATED: EventSpec(_DOMAIN, _INFO),
        PREDIO_GEOMETRY_CHANGED: EventSpec(_DOMAIN, _WARNING),
        PREDIO_DELETED: EventSpec(_DOMAIN, _WARNING),
        PREDIO_USER_ASSIGNED: EventSpec(_SECURITY, _WARNING),
        PREDIO_USER_UNASSIGNED: EventSpec(_SECURITY, _WARNING),
        LOTE_CREATED: EventSpec(_DOMAIN, _INFO),
        LOTE_UPDATED: EventSpec(_DOMAIN, _INFO),
        LOTE_GEOMETRY_CHANGED: EventSpec(_DOMAIN, _WARNING),
        LOTE_DELETED: EventSpec(_DOMAIN, _WARNING),
        LOTES_IMPORTED: EventSpec(_DOMAIN, _INFO),
        # Under the threshold, accepted without asking: routine.
        GEOMETRY_REPAIRED: EventSpec(_DOMAIN, _INFO),
        # Someone explicitly stored a repair the system found suspicious: it
        # must be searchable on its own, apart from routine repairs.
        GEOMETRY_REPAIR_ACCEPTED: EventSpec(_DOMAIN, _WARNING),
        # Returned for confirmation. Many in a row from one user means the
        # threshold is fighting them: that is what this event is for.
        GEOMETRY_REPAIR_REJECTED: EventSpec(_DOMAIN, _INFO),
    }
)

# Satellite analysis (Phase 3). Details carry scene date, cloud cover, PU,
# reflectance percentiles, anomaly counts and durations; never rasters and
# never full geometries.
ANALYSIS_REQUESTED: Final = "ANALYSIS_REQUESTED"
ANALYSIS_STARTED: Final = "ANALYSIS_STARTED"
ANALYSIS_COMPLETED: Final = "ANALYSIS_COMPLETED"
ANALYSIS_FAILED: Final = "ANALYSIS_FAILED"
ANALYSIS_NO_SUITABLE_SCENE: Final = "ANALYSIS_NO_SUITABLE_SCENE"
ANALYSIS_RESOLVED_TO_EXISTING: Final = "ANALYSIS_RESOLVED_TO_EXISTING"
ANALYSIS_FORCED_RECOMPUTE: Final = "ANALYSIS_FORCED_RECOMPUTE"
ANOMALY_DETECTED: Final = "ANOMALY_DETECTED"
ANOMALY_REVIEWED: Final = "ANOMALY_REVIEWED"
RASTER_DOWNLOADED: Final = "RASTER_DOWNLOADED"

_SYSTEM = AuditEventCategory.SYSTEM
_ERROR = AuditEventSeverity.ERROR

ANALYSIS_EVENTS: Final[Mapping[str, EventSpec]] = MappingProxyType(
    {
        ANALYSIS_REQUESTED: EventSpec(_DOMAIN, _INFO),
        ANALYSIS_STARTED: EventSpec(_DOMAIN, _INFO),
        ANALYSIS_COMPLETED: EventSpec(_DOMAIN, _INFO),
        # A system failure, unlike a cloudy sky.
        ANALYSIS_FAILED: EventSpec(_DOMAIN, _WARNING),
        ANALYSIS_NO_SUITABLE_SCENE: EventSpec(_DOMAIN, _INFO),
        # The scene already had a result: the request resolved to it, at no
        # download cost.
        ANALYSIS_RESOLVED_TO_EXISTING: EventSpec(_DOMAIN, _INFO),
        # A result was superseded: quota spent twice on one scene, and its
        # reviewed anomalies stay behind on the old analysis.
        ANALYSIS_FORCED_RECOMPUTE: EventSpec(_DOMAIN, _WARNING),
        ANOMALY_DETECTED: EventSpec(_DOMAIN, _INFO),
        ANOMALY_REVIEWED: EventSpec(_DOMAIN, _INFO),
        RASTER_DOWNLOADED: EventSpec(_DOMAIN, _INFO),
        # The budget is gone: nothing more can run this month.
        SENTINEL_QUOTA_EXCEEDED: EventSpec(_SYSTEM, _ERROR),
        SENTINEL_CIRCUIT_OPEN: EventSpec(_SYSTEM, _WARNING),
    }
)
