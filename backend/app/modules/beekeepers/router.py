"""HTTP surface of the beekeepers context."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/beekeepers", tags=["beekeepers"])
# Endpoints are implemented in later phases.
