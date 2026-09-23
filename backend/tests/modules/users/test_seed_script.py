"""The development seeding script must never run where it could do harm."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.shared.roles import Role

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = REPO_ROOT / "scripts" / "seed_dev_users.py"

STRONG_PASSWORD = "Cordillera-Sur-2026"  # noqa: S105 - test fixture, not a credential


@pytest.fixture(scope="module")
def seeder() -> ModuleType:
    spec = importlib.util.spec_from_file_location("seed_dev_users", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _settings_stub(environment: str) -> Any:
    class _Stub:
        def __init__(self) -> None:
            self.environment = environment

    return lambda: _Stub()


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_it_refuses_to_run_outside_development(
    seeder: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
) -> None:
    monkeypatch.setattr(seeder, "get_settings", _settings_stub(environment))
    monkeypatch.setenv("SEED_PASSWORD", STRONG_PASSWORD)

    assert seeder.main([]) == 1


def test_it_requires_a_password_from_the_environment(
    seeder: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(seeder, "get_settings", _settings_stub("development"))
    monkeypatch.delenv("SEED_PASSWORD", raising=False)

    # No hardcoded fallback: without the variable the script does nothing.
    assert seeder.main([]) == 1


def test_it_rejects_a_password_that_fails_the_policy(
    seeder: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(seeder, "get_settings", _settings_stub("development"))
    monkeypatch.setenv("SEED_PASSWORD", "corto")

    assert seeder.main([]) == 1


def test_it_covers_every_role(seeder: ModuleType) -> None:
    emails = {seeder.email_for(role) for role in Role}
    assert len(emails) == len(Role) == 9
    assert all(email.endswith("@lar.local") for email in emails)


def test_the_script_contains_no_hardcoded_password(seeder: ModuleType) -> None:
    source = SCRIPT_PATH.read_text(encoding="utf-8")

    # The password must come from the environment, never from a literal.
    assert "os.environ.get(PASSWORD_VARIABLE)" in source
    assignment = re.search(r"password\s*=\s*[\"']", source, re.IGNORECASE)
    assert assignment is None, f"literal password assignment: {assignment}"
