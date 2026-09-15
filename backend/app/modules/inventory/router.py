"""HTTP surface of the inventory context."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/inventory", tags=["inventory"])
# Endpoints are implemented in later phases.
