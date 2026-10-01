"""Generate frontend constants for backend enums shared with the UI.

Sibling of ``generate_frontend_roles.py``, with the same ``--check`` contract.

Usage:
    python scripts/generate_frontend_enums.py            # write the file
    python scripts/generate_frontend_enums.py --check    # fail on drift (CI)
"""

from __future__ import annotations

import argparse
import difflib
import sys
from enum import StrEnum
from pathlib import Path
from typing import Any, NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_PATH = REPO_ROOT / "backend"
if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))

from app.shared.enums import (  # noqa: E402
    ANALYSIS_STATUS_LABELS,
    ANALYSIS_TYPE_LABELS,
    ANOMALY_REVIEW_STATUS_LABELS,
    ANOMALY_SEVERITY_LABELS,
    LOTE_STATS_EXCLUSION_LABELS,
    LOTE_TYPE_LABELS,
    AnalysisStatus,
    AnalysisType,
    AnomalyReviewStatus,
    AnomalySeverity,
    LoteStatsExclusion,
    LoteType,
)

OUTPUT_PATH = REPO_ROOT / "frontend" / "src" / "config" / "enums.generated.ts"

HEADER = """// AUTO-GENERATED — DO NOT EDIT BY HAND.
// Source: backend/app/shared/enums.py
// Regenerate: python scripts/generate_frontend_enums.py
"""


class SharedEnum(NamedTuple):
    """A backend enum exported to the frontend, with its display labels.

    A NamedTuple rather than a dataclass: the tests load this script by path,
    without registering it in sys.modules, which dataclasses require.
    """

    enum: type[StrEnum]
    labels: dict[Any, str]
    values_name: str
    type_name: str
    labels_name: str


# Every enum the UI needs, in the order they appear in the generated file.
SHARED_ENUMS: tuple[SharedEnum, ...] = (
    SharedEnum(LoteType, LOTE_TYPE_LABELS, "LOTE_TYPES", "LoteType", "LOTE_TYPE_LABELS"),
    SharedEnum(
        AnalysisType,
        ANALYSIS_TYPE_LABELS,
        "ANALYSIS_TYPES",
        "AnalysisType",
        "ANALYSIS_TYPE_LABELS",
    ),
    SharedEnum(
        AnalysisStatus,
        ANALYSIS_STATUS_LABELS,
        "ANALYSIS_STATUSES",
        "AnalysisStatus",
        "ANALYSIS_STATUS_LABELS",
    ),
    SharedEnum(
        AnomalySeverity,
        ANOMALY_SEVERITY_LABELS,
        "ANOMALY_SEVERITIES",
        "AnomalySeverity",
        "ANOMALY_SEVERITY_LABELS",
    ),
    SharedEnum(
        AnomalyReviewStatus,
        ANOMALY_REVIEW_STATUS_LABELS,
        "ANOMALY_REVIEW_STATUSES",
        "AnomalyReviewStatus",
        "ANOMALY_REVIEW_STATUS_LABELS",
    ),
    SharedEnum(
        LoteStatsExclusion,
        LOTE_STATS_EXCLUSION_LABELS,
        "LOTE_STATS_EXCLUSIONS",
        "LoteStatsExclusion",
        "LOTE_STATS_EXCLUSION_LABELS",
    ),
)


def _ts_string(value: str) -> str:
    """Render a Python string as a single-quoted TypeScript literal."""
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _render_enum(shared: SharedEnum) -> str:
    """Render one enum: its values, its union type and its labels."""
    values, type_name = shared.values_name, shared.type_name
    parts: list[str] = [f"\nexport const {values} = {{\n"]
    for member in shared.enum:
        parts.append(f"  {member.name}: {_ts_string(member.value)},\n")
    parts.append("} as const\n")
    union = f"(typeof {values})[keyof typeof {values}]"
    parts.append(f"\nexport type {type_name} = {union}\n")
    parts.append(
        f"\nexport const {shared.labels_name}: Record<{type_name}, string> = {{\n"
    )
    for member in shared.enum:
        parts.append(f"  {member.name}: {_ts_string(shared.labels[member])},\n")
    parts.append("}\n")
    return "".join(parts)


def render() -> str:
    """Render the TypeScript module. Deterministic: ordering follows the enums."""
    return HEADER + "".join(_render_enum(shared) for shared in SHARED_ENUMS)


def current() -> str | None:
    """Return the contents of the generated file, or None when missing."""
    if not OUTPUT_PATH.exists():
        return None
    return OUTPUT_PATH.read_text(encoding="utf-8")


def write() -> bool:
    """Write the generated file. Returns True when the contents changed."""
    expected = render()
    if current() == expected:
        return False
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(expected, encoding="utf-8", newline="\n")
    return True


def diff() -> str:
    """Return a unified diff between the file on disk and the expected output."""
    return "".join(
        difflib.unified_diff(
            (current() or "").splitlines(keepends=True),
            render().splitlines(keepends=True),
            fromfile=f"{OUTPUT_PATH.name} (on disk)",
            tofile=f"{OUTPUT_PATH.name} (expected)",
        )
    )


def check() -> bool:
    """Return True when the file on disk matches the backend enums."""
    return current() == render()


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 when the generated file is out of date",
    )
    args = parser.parse_args(argv)

    if args.check:
        if check():
            print(f"{OUTPUT_PATH.relative_to(REPO_ROOT)} is up to date")
            return 0
        print(
            f"{OUTPUT_PATH.relative_to(REPO_ROOT)} is out of date.\n"
            "Run: python scripts/generate_frontend_enums.py",
            file=sys.stderr,
        )
        print(diff(), file=sys.stderr)
        return 1

    status = "updated" if write() else "unchanged"
    print(f"{OUTPUT_PATH.relative_to(REPO_ROOT)} {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
