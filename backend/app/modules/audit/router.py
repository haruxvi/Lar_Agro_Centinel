"""HTTP surface of the audit context."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/audit", tags=["audit"])
