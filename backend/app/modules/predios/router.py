"""HTTP surface of the predios context."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/predios", tags=["predios"])
