"""HTTP surface of the warehouses context."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/warehouses", tags=["warehouses"])
# Endpoints are implemented in later phases.
