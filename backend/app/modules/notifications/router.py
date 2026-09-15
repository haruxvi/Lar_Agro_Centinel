"""HTTP surface of the notifications context."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/notifications", tags=["notifications"])
# Endpoints are implemented in later phases.
