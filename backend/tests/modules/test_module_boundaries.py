"""Every bounded context exposes the same public surface."""

from __future__ import annotations

import importlib

import pytest
from fastapi import APIRouter

MODULES = ["auth", "predios", "analysis", "audit", "health"]


@pytest.mark.parametrize("name", MODULES)
def test_module_exports_router(name: str) -> None:
    module = importlib.import_module(f"app.modules.{name}")
    assert isinstance(module.router, APIRouter)


@pytest.mark.parametrize("name", ["auth", "predios", "analysis", "audit"])
def test_module_has_layered_structure(name: str) -> None:
    for layer in ("router", "service", "repository", "schema", "models"):
        importlib.import_module(f"app.modules.{name}.{layer}")
