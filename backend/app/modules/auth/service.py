"""Application services of the auth context.

Authentication rules are added once the corresponding features are specified;
this class only wires the dependency graph.
"""

from __future__ import annotations

from app.modules.auth.repository import UserRepository


class AuthService:
    """Entry point of the auth context's public API."""

    def __init__(self, repository: UserRepository) -> None:
        """Bind the service to its repository."""
        self._repository = repository
