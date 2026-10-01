"""Last line of defence: the suite never runs with real Sentinel Hub access.

Layer 1 lives in conftest.py and blanks every Sentinel credential before any
settings are built; it is what actually protects. These tests prove it holds,
and raise the alarm when credentials were present in the process environment,
which is how CI would inject them.
"""

from __future__ import annotations

import pytest

from app.modules.analysis.availability import analysis_available
from app.modules.analysis.sentinel_client import SentinelAuthError, SentinelClient
from app.shared.config import get_settings


def test_no_sentinel_credentials_in_the_process_environment(
    sentinel_variables_in_process_env: tuple[str, ...],
) -> None:
    found = sentinel_variables_in_process_env
    names = " ".join(found)
    assert not found, (
        f"{', '.join(found)} está en el entorno del proceso. La suite nunca debe "
        "correr con credenciales reales de Sentinel Hub disponibles.\n"
        f"Ejecutá: unset {names}\n"
        "Las credenciales en .env NO causan este error."
    )


def test_credentials_in_dotenv_never_reach_the_tests() -> None:
    # Whatever a developer's .env holds, the settings the suite sees have none.
    settings = get_settings()
    assert settings.sentinel_configured is False
    assert analysis_available(settings) is False


def test_no_real_client_can_be_built_inside_the_suite() -> None:
    with pytest.raises(SentinelAuthError, match="not configured"):
        SentinelClient(get_settings(), redis=None, events=None)  # type: ignore[arg-type]
