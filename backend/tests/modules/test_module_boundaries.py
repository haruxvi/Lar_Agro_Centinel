"""Every bounded context exposes the same public surface."""

from __future__ import annotations

import importlib

import pytest
from fastapi import APIRouter

MODULES = [
    "auth",
    "users",
    "predios",
    "devices",
    "captures",
    "analysis",
    "warehouses",
    "inventory",
    "products",
    "operations",
    "responsible_mode",
    "audit",
    "notifications",
    "beekeepers",
    "health",
]

LAYERS = ("router", "service", "repository", "schema", "models")

EXTENSIONS = [
    "auth.two_factor",
    "auth.security",
    "devices.adapters.base",
    "devices.adapters.manual_upload",
    "devices.adapters.dji_sdk",
    "devices.adapters.mavlink",
    "analysis.ndvi",
    "analysis.anomaly",
    "analysis.rgb_indices",
    "analysis.segmentation",
    "responsible_mode.policies.drone_policy",
    "responsible_mode.policies.application_policy",
    "responsible_mode.policies.personnel_policy",
    "audit.hash_chain",
]


@pytest.mark.parametrize("name", MODULES)
def test_module_exports_router(name: str) -> None:
    module = importlib.import_module(f"app.modules.{name}")
    assert isinstance(module.router, APIRouter)


@pytest.mark.parametrize("name", MODULES)
def test_module_has_layered_structure(name: str) -> None:
    for layer in LAYERS:
        importlib.import_module(f"app.modules.{name}.{layer}")


@pytest.mark.parametrize("dotted", EXTENSIONS)
def test_module_extensions_are_importable(dotted: str) -> None:
    importlib.import_module(f"app.modules.{dotted}")
