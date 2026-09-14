"""HTTP surface of the analysis context."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/analysis", tags=["analysis"])
