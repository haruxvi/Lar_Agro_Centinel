"""Generate the frontend role constants from the backend role catalogue.

Usage:
    python scripts/generate_frontend_roles.py            # write the file
    python scripts/generate_frontend_roles.py --check    # fail on drift (CI)
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

from app.shared.roles import ROLE_META, ROLES_2FA_REQUIRED, Role  # noqa: E402

OUTPUT_PATH = REPO_ROOT / "frontend" / "src" / "config" / "roles.generated.ts"

HEADER = """// AUTO-GENERATED — DO NOT EDIT BY HAND.
// Source: backend/app/shared/roles.py
// Regenerate: python scripts/generate_frontend_roles.py
"""


def _ts_string(value: str) -> str:
    """Render a Python string as a single-quoted TypeScript literal."""
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def render() -> str:
    """Render the TypeScript module. Deterministic: ordering follows the enum."""
    roles = list(Role)

    parts: list[str] = [HEADER, "\nexport const ROLES = {\n"]
    for role in roles:
        parts.append(f"  {role.name}: {_ts_string(role.value)},\n")
    parts.append("} as const\n")

    parts.append("\nexport type Role = (typeof ROLES)[keyof typeof ROLES]\n")

    parts.append(
        "\nexport interface RoleMeta {\n"
        "  label: string\n"
        "  shortLabel: string\n"
        "  color: string\n"
        "  description: string\n"
        "}\n"
    )

    parts.append("\nexport const ROLE_META: Record<Role, RoleMeta> = {\n")
    for role in roles:
        meta = ROLE_META[role]
        parts.append(
            f"  {role.name}: {{\n"
            f"    label: {_ts_string(meta.label)},\n"
            f"    shortLabel: {_ts_string(meta.short_label)},\n"
            f"    color: {_ts_string(meta.color)},\n"
            f"    description: {_ts_string(meta.description)},\n"
            f"  }},\n"
        )
    parts.append("}\n")

    # Iterate the enum rather than the frozenset: set iteration order is not
    # stable across processes, which would make the output non-deterministic.
    parts.append(
        "\nexport const ROLES_2FA_REQUIRED: ReadonlySet<Role> = new Set<Role>([\n"
    )
    for role in roles:
        if role in ROLES_2FA_REQUIRED:
            parts.append(f"  ROLES.{role.name},\n")
    parts.append("])\n")

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
    existing = current()
    return "".join(
        difflib.unified_diff(
            (existing or "").splitlines(keepends=True),
            render().splitlines(keepends=True),
            fromfile=f"{OUTPUT_PATH.name} (on disk)",
            tofile=f"{OUTPUT_PATH.name} (expected)",
        )
    )


def check() -> bool:
    """Return True when the file on disk matches the backend catalogue."""
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
            "Run: python scripts/generate_frontend_roles.py",
            file=sys.stderr,
        )
        print(diff(), file=sys.stderr)
        return 1

    changed = write()
    status = "updated" if changed else "unchanged"
    print(f"{OUTPUT_PATH.relative_to(REPO_ROOT)} {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
