"""Domain errors of the predios context.

The service raises these; only the router translates them to HTTP. Each
``as_dict`` is sent to the client, so it carries identifiers and measurements
of the caller's own data and never a geometry or another tenant's data.

Invalid geometries raise ``app.shared.geo.InvalidGeometryError``, which is
shared with every module that accepts GeoJSON.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from app.shared.exceptions import DomainError
from app.shared.geo import RepairAssessment, RepairVerdict


class PredioError(DomainError):
    """Base class for predios errors."""


class PredioNotFoundError(PredioError):
    """The predio does not exist, or has been deleted."""


class PredioAccessDeniedError(PredioError):
    """The actor holds no grant that allows this action on the predio."""


class InvalidSlugError(PredioError):
    """A slug that is not lowercase words joined by hyphens."""


class SlugConflictError(PredioError):
    """The owner already has a live predio with this slug."""

    def __init__(self, slug: str) -> None:
        """Record the conflicting slug."""
        super().__init__(slug)
        self.slug = slug

    def as_dict(self) -> dict[str, object]:
        """Return the conflicting slug."""
        return {"error": type(self).__name__, "slug": self.slug}


class AreaOutOfBoundsError(PredioError):
    """The predio's area falls outside the configured plausible range."""

    def __init__(self, area_m2: float, min_m2: float, max_m2: float) -> None:
        """Record the measured area and the accepted range."""
        super().__init__(f"area {area_m2:.1f} m2 outside [{min_m2}, {max_m2}]")
        self.area_m2 = area_m2
        self.min_m2 = min_m2
        self.max_m2 = max_m2

    def as_dict(self) -> dict[str, object]:
        """Return the measured area and the accepted range."""
        return {
            "error": type(self).__name__,
            "area_m2": round(self.area_m2, 2),
            "min_m2": self.min_m2,
            "max_m2": self.max_m2,
        }


class PredioHasActiveLotesError(PredioError):
    """A predio cannot be deleted while it still has live lotes."""

    def __init__(self, active_lotes: int) -> None:
        """Record how many live lotes block the deletion."""
        super().__init__(f"{active_lotes} active lotes")
        self.active_lotes = active_lotes

    def as_dict(self) -> dict[str, object]:
        """Return the number of live lotes."""
        return {"error": type(self).__name__, "active_lotes": self.active_lotes}


class LoteNotFoundError(PredioError):
    """The lote does not exist in this predio, or has been deleted.

    Also raised for a lote that exists under another predio: from the caller's
    point of view it does not exist, and saying otherwise would leak it.
    """


class LoteNameConflictError(PredioError):
    """The predio already has a live lote with this name."""

    def __init__(self, name: str) -> None:
        """Record the conflicting name."""
        super().__init__(name)
        self.name = name

    def as_dict(self) -> dict[str, object]:
        """Return the conflicting name."""
        return {"error": type(self).__name__, "name": self.name}


class LoteNotContainedError(PredioError):
    """A lote extends beyond its predio by more than the tolerance."""

    def __init__(
        self,
        outside_m2: float,
        tolerance_m: float,
        lote_id: uuid.UUID | None = None,
    ) -> None:
        """Record how much of the lote falls outside, and which lote if known."""
        super().__init__(f"{outside_m2:.1f} m2 outside the predio")
        self.outside_m2 = outside_m2
        self.tolerance_m = tolerance_m
        self.lote_id = lote_id

    def as_dict(self) -> dict[str, object]:
        """Return the area outside the predio and the tolerance applied."""
        payload: dict[str, object] = {
            "error": type(self).__name__,
            "outside_m2": round(self.outside_m2, 2),
            "tolerance_m": self.tolerance_m,
        }
        if self.lote_id is not None:
            payload["lote_id"] = str(self.lote_id)
        return payload


class LotesOverlapError(PredioError):
    """A lote shares area with sibling lotes of the same predio."""

    def __init__(self, overlaps: Sequence[tuple[uuid.UUID, float]]) -> None:
        """Record each overlapping sibling and the shared area in m2."""
        super().__init__(f"overlaps {len(overlaps)} lotes")
        self.overlaps = list(overlaps)

    def as_dict(self) -> dict[str, object]:
        """Return the overlapping siblings and the shared area."""
        return {
            "error": type(self).__name__,
            "overlaps": [
                {"lote_id": str(lote_id), "overlap_m2": round(area, 2)}
                for lote_id, area in self.overlaps
            ],
        }


class GeoJSONImportError(PredioError):
    """A GeoJSON import was rejected as a whole; nothing was written."""

    def __init__(self, errors: Sequence[dict[str, object]]) -> None:
        """Record the error of every rejected feature, by index."""
        super().__init__(f"{len(errors)} features rejected")
        self.errors = list(errors)

    def as_dict(self) -> dict[str, object]:
        """Return every feature's error, by index."""
        return {"error": type(self).__name__, "features": self.errors}


# --- predio membership --------------------------------------------------------


class MemberNotFoundError(PredioError):
    """No active account matches the user to assign.

    The same error covers an unknown and a deactivated account, so the
    response does not tell them apart.
    """


class RoleNotAssignableError(PredioError):
    """The role cannot be granted on a predio."""

    def __init__(self, role: str, reason: str) -> None:
        """Record the refused role and why."""
        super().__init__(f"{role}: {reason}")
        self.role = role
        self.reason = reason

    def as_dict(self) -> dict[str, object]:
        """Return the refused role and the reason."""
        return {"error": type(self).__name__, "role": self.role, "reason": self.reason}


class AssignmentExistsError(PredioError):
    """The user already holds this role on the predio."""

    def __init__(self, role: str) -> None:
        """Record the role already held."""
        super().__init__(role)
        self.role = role

    def as_dict(self) -> dict[str, object]:
        """Return the role already held."""
        return {"error": type(self).__name__, "role": self.role}


class AssignmentNotFoundError(PredioError):
    """The user does not hold this role on the predio."""


class LastPropietarioError(PredioError):
    """Removing this grant would leave the predio without a PROPIETARIO."""


# --- geometry repair ----------------------------------------------------------


class GeometryRepairExceedsThresholdError(PredioError):
    """An automatic repair needs explicit confirmation before it is stored.

    Raised when the repair changes the area beyond the threshold, or when its
    effect cannot be measured at all. The payload carries the repaired
    geometry so the client can draw it over the original: the system does not
    know which shape matches the real predio, the person does. Resending the
    request with ``accept_repair: true`` stores the repair.
    """

    def __init__(
        self, assessment: RepairAssessment, repaired_geometry: dict[str, Any]
    ) -> None:
        """Record the assessment and the repaired geometry to preview."""
        super().__init__(assessment.verdict.value)
        self.assessment = assessment
        self.repaired_geometry = repaired_geometry

    @property
    def message(self) -> str:
        """Explain, in the user's language, why confirmation is needed."""
        verdict = self.assessment.verdict
        if verdict is RepairVerdict.NOT_MEASURABLE_SELF_INTERSECTION:
            return (
                "La geometría enviada tiene un borde que se cruza consigo mismo. "
                "El sistema no puede saber si quisiste el contorno exterior o las "
                "piezas que forma el cruce. Revisá la geometría reparada y "
                "confirmá si es correcta."
            )
        if verdict is RepairVerdict.NOT_MEASURABLE_ZERO_AREA:
            return (
                "La geometría enviada es inválida y su área original no se puede "
                "medir. Revisá la geometría reparada y confirmá si es correcta."
            )
        ratio = self.assessment.change_ratio or 0.0
        return (
            "La geometría enviada es inválida. La reparación automática cambia "
            f"el área en {abs(ratio) * 100:.1f}%. Revisá la geometría reparada y "
            "confirmá si es correcta."
        )

    def as_dict(self) -> dict[str, object]:
        """Return the figures, the repaired geometry and a readable message."""
        return {
            "error": "GeometryRepairExceedsThreshold",
            "detail": {
                **self.assessment.as_details(),
                "repaired_geometry": self.repaired_geometry,
                "message": self.message,
            },
        }


class OwnerRoleProtectedError(PredioError):
    """The predio owner's PROPIETARIO role cannot be revoked.

    Otherwise a co-owner the owner invited could lock the owner out of their
    own predio. It stays until ownership transfer exists as its own, audited
    operation.
    """
