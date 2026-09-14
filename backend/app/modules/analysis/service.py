"""Application services of the analysis context."""

from __future__ import annotations

from app.modules.analysis.repository import AnalysisRepository


class AnalysisService:
    """Entry point of the analysis context's public API."""

    def __init__(self, repository: AnalysisRepository) -> None:
        """Bind the service to its repository."""
        self._repository = repository
