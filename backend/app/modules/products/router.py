"""HTTP surface of the products context."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/products", tags=["products"])
# Endpoints are implemented in later phases.
