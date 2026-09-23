"""The generated frontend role constants must stay in sync with the backend."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.shared.roles import ROLE_META, ROLES_2FA_REQUIRED, Role

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "generate_frontend_roles.py"


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_frontend_roles", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generator() -> ModuleType:
    return _load_generator()


def test_output_is_deterministic(generator: ModuleType) -> None:
    assert generator.render() == generator.render()


def test_output_carries_the_do_not_edit_header(generator: ModuleType) -> None:
    first_line = generator.render().splitlines()[0]
    assert "AUTO-GENERATED" in first_line
    assert "DO NOT EDIT" in first_line


def test_output_contains_every_role(generator: ModuleType) -> None:
    rendered = generator.render()
    for role in Role:
        assert f"  {role.name}: '{role.value}'," in rendered
        assert ROLE_META[role].label in rendered
        assert ROLE_META[role].short_label in rendered


def test_output_lists_two_factor_roles_in_enum_order(generator: ModuleType) -> None:
    rendered = generator.render()
    block = rendered.split("ROLES_2FA_REQUIRED")[1]
    listed = [role for role in Role if f"ROLES.{role.name}," in block]
    assert listed == [role for role in Role if role in ROLES_2FA_REQUIRED]


def test_generated_file_on_disk_is_in_sync(generator: ModuleType) -> None:
    assert generator.check(), generator.diff()


def test_check_detects_drift(generator: ModuleType) -> None:
    original = generator.OUTPUT_PATH.read_text(encoding="utf-8")
    try:
        generator.OUTPUT_PATH.write_text(
            original.replace("Propietario", "Dueño"), encoding="utf-8", newline="\n"
        )
        assert generator.check() is False
    finally:
        generator.OUTPUT_PATH.write_text(original, encoding="utf-8", newline="\n")
    assert generator.check() is True


def test_cli_check_exits_zero_when_in_sync() -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT_PATH), "--check"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stderr
