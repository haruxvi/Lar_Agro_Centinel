"""HTTP surface of the captures context."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/captures", tags=["captures"])
# Endpoints are implemented in later phases.
