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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_PATH = REPO_ROOT / "backend"
if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))

from app.shared.enums import LOTE_TYPE_LABELS, LoteType  # noqa: E402

OUTPUT_PATH = REPO_ROOT / "frontend" / "src" / "config" / "enums.generated.ts"

HEADER = """// AUTO-GENERATED — DO NOT EDIT BY HAND.
// Source: backend/app/shared/enums.py
// Regenerate: python scripts/generate_frontend_enums.py
"""


def _ts_string(value: str) -> str:
    """Render a Python string as a single-quoted TypeScript literal."""
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def render() -> str:
    """Render the TypeScript module. Deterministic: ordering follows the enum."""
    parts: list[str] = [HEADER, "\nexport const LOTE_TYPES = {\n"]
    for member in LoteType:
        parts.append(f"  {member.name}: {_ts_string(member.value)},\n")
    parts.append("} as const\n")
    parts.append(
        "\nexport type LoteType = (typeof LOTE_TYPES)[keyof typeof LOTE_TYPES]\n"
    )
    parts.append("\nexport const LOTE_TYPE_LABELS: Record<LoteType, string> = {\n")
    for member in LoteType:
        parts.append(f"  {member.name}: {_ts_string(LOTE_TYPE_LABELS[member])},\n")
    parts.append("}\n")
    return "".join(parts)


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
