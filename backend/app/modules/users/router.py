"""HTTP surface of the users context."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/users", tags=["users"])
# Endpoints are implemented in later phases.
