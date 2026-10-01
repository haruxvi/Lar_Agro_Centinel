"""Whether satellite analysis can run on this deployment.

Missing Sentinel Hub credentials do not stop the application: everything
else must be developable and runnable without satellite access. Instead the
analysis module reports itself unavailable, and its endpoints answer 503.
"""

from __future__ import annotations

from app.modules.analysis.exceptions import AnalysisUnavailableError
from app.shared.config import Settings, get_settings


def analysis_available(settings: Settings | None = None) -> bool:
    """Return whether the Sentinel Hub credentials are configured."""
    return (settings or get_settings()).sentinel_configured


def ensure_analysis_available(settings: Settings | None = None) -> None:
    """Raise unless satellite analysis can run."""
    if not analysis_available(settings):
        raise AnalysisUnavailableError
