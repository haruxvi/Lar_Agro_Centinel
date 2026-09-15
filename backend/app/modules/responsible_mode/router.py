"""HTTP surface of the responsible mode context."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/responsible-mode", tags=["responsible-mode"])
# Endpoints are implemented in later phases.
