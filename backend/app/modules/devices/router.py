"""HTTP surface of the devices context."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/devices", tags=["devices"])
# Endpoints are implemented in later phases.
