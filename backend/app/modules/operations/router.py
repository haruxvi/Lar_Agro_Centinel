"""HTTP surface of the operations context."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/operations", tags=["operations"])
# Endpoints are implemented in later phases.
