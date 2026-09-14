"""Application services of the predios context."""

from __future__ import annotations

from app.modules.predios.repository import PredioRepository


class PredioService:
    """Entry point of the predios context's public API."""

    def __init__(self, repository: PredioRepository) -> None:
        """Bind the service to its repository."""
        self._repository = repository
