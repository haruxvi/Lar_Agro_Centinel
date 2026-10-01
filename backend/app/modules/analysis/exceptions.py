"""Domain errors of the analysis context.

The service and the worker raise these; only the router translates them to
HTTP. Each ``as_dict`` reaches the client, so it never carries credentials,
rasters or geometries.
"""

from __future__ import annotations

import uuid

from app.shared.exceptions import DomainError


class AnalysisError(DomainError):
    """Base class for analysis errors."""


class AnalysisUnavailableError(AnalysisError):
    """Satellite analysis is not configured on this deployment.

    Raised when the Sentinel Hub credentials are missing. The rest of the
    system keeps working: only analysis is switched off.
    """

    def as_dict(self) -> dict[str, object]:
        """Explain what is missing, without naming any secret value."""
        return {
            "error": type(self).__name__,
            "message": (
                "El análisis satelital no está disponible: faltan las "
                "credenciales de Sentinel Hub en la configuración del servidor."
            ),
        }


class AnalysisNotFoundError(AnalysisError):
    """The analysis does not exist in this predio."""


class AnalysisAlreadyActiveError(AnalysisError):
    """An analysis of this type is already pending or running for the predio.

    Carries the id of the one in flight, so the client can poll it instead
    of retrying.
    """

    def __init__(self, active_analysis_id: uuid.UUID | None) -> None:
        """Record which analysis is in the way (None if it finished meanwhile)."""
        super().__init__(str(active_analysis_id))
        self.active_analysis_id = active_analysis_id

    def as_dict(self) -> dict[str, object]:
        """Point at the analysis in flight."""
        return {
            "error": type(self).__name__,
            "message": "Ya hay un análisis de este tipo en curso para el predio.",
            "analysis_id": (
                str(self.active_analysis_id) if self.active_analysis_id else None
            ),
        }


class AnomalyNotFoundError(AnalysisError):
    """The anomaly does not exist in this predio."""


class ArtifactNotAvailableError(AnalysisError):
    """The analysis has no raster or preview to serve (not completed, or lost).

    A DUPLICATE_SCENE analysis has none of its own: the answer points at the
    analysis that holds them.
    """

    def __init__(self, resolved_to_analysis_id: uuid.UUID | None) -> None:
        """Record where the artifacts are, when they are elsewhere."""
        super().__init__(str(resolved_to_analysis_id))
        self.resolved_to_analysis_id = resolved_to_analysis_id

    def as_dict(self) -> dict[str, object]:
        """Say there is nothing here, and where to look instead."""
        payload: dict[str, object] = {
            "error": type(self).__name__,
            "message": "Este análisis no tiene un raster disponible.",
        }
        if self.resolved_to_analysis_id is not None:
            payload["resolved_to_analysis_id"] = str(self.resolved_to_analysis_id)
        return payload


class InvalidDateRangeError(AnalysisError):
    """The requested date range cannot be analysed."""

    def __init__(self, reason: str) -> None:
        """Record why the range was refused (a Spanish, user-facing reason)."""
        super().__init__(reason)
        self.reason = reason

    def as_dict(self) -> dict[str, object]:
        """Say what is wrong with the range."""
        return {"error": type(self).__name__, "message": self.reason}


class PredioTooLargeError(AnalysisError):
    """The predio exceeds the area one analysis may cover."""

    def __init__(self, area_m2: float, max_area_m2: float) -> None:
        """Record the predio's area and the limit."""
        super().__init__(f"{area_m2:.0f} m2 > {max_area_m2:.0f} m2")
        self.area_m2 = area_m2
        self.max_area_m2 = max_area_m2

    def as_dict(self) -> dict[str, object]:
        """Give both areas, in hectares, so the user sees by how much."""
        return {
            "error": type(self).__name__,
            "message": "El predio supera el área máxima que cubre un análisis.",
            "area_ha": round(self.area_m2 / 10_000, 2),
            "max_area_ha": round(self.max_area_m2 / 10_000, 2),
        }


class QueueUnavailableError(AnalysisError):
    """The analysis could not be handed to the worker queue."""

    def as_dict(self) -> dict[str, object]:
        """Explain without exposing the queue's address."""
        return {
            "error": type(self).__name__,
            "message": (
                "No se pudo encolar el análisis; intentá de nuevo en unos minutos."
            ),
        }
