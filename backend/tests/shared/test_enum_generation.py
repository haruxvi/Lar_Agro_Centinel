"""The generated frontend enum constants must stay in sync with the backend."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.shared.enums import LOTE_TYPE_LABELS, LoteType

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "generate_frontend_enums.py"


@pytest.fixture(scope="module")
def generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_frontend_enums", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_lote_type_has_a_label() -> None:
    assert set(LOTE_TYPE_LABELS) == set(LoteType)
    assert all(label.strip() for label in LOTE_TYPE_LABELS.values())


def test_output_is_deterministic(generator: ModuleType) -> None:
    assert generator.render() == generator.render()


def test_output_contains_every_lote_type_and_label(generator: ModuleType) -> None:
    rendered = generator.render()
    assert "AUTO-GENERATED" in rendered.splitlines()[0]
    for member in LoteType:
        assert f"  {member.name}: '{member.value}'," in rendered
        assert LOTE_TYPE_LABELS[member] in rendered


def test_generated_file_on_disk_is_in_sync(generator: ModuleType) -> None:
    assert generator.check(), generator.diff()


def test_check_detects_drift(generator: ModuleType) -> None:
    original = generator.OUTPUT_PATH.read_text(encoding="utf-8")
    try:
        generator.OUTPUT_PATH.write_text(
            original.replace("Potrero", "Pradera"), encoding="utf-8", newline="\n"
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
