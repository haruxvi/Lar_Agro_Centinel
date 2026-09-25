"""Create one development user per role.

Refuses to run outside development, and takes the password from the
environment so no credential is ever written into the repository.

Usage:
    SEED_PASSWORD='...' python scripts/seed_dev_users.py
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_PATH = REPO_ROOT / "backend"
if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))

from app.modules.auth.security import (  # noqa: E402
    hash_password,
    validate_password_policy,
)
from app.modules.users.models import User, UserRole  # noqa: E402
from app.shared.config import get_settings  # noqa: E402
from app.shared.db import get_session_factory  # noqa: E402
from app.shared.roles import ROLE_META, Role  # noqa: E402

EMAIL_DOMAIN = "lar.local"
PASSWORD_VARIABLE = "SEED_PASSWORD"  # noqa: S105 - variable name, not a secret


def email_for(role: Role) -> str:
    """Return the development email used for a role."""
    return f"{role.value.lower()}@{EMAIL_DOMAIN}"


def seed(password: str, *, dry_run: bool = False) -> int:
    """Create the missing development users. Returns how many were created."""
    created = 0
    session = get_session_factory()()
    try:
        for role in Role:
            email = email_for(role)
            existing = session.query(User).filter(User.email == email).one_or_none()
            if existing is not None:
                print(f"  exists   {email}")
                continue
            if dry_run:
                print(f"  would create {email}")
                continue

            user = User(
                email=email,
                password_hash=hash_password(password),
                full_name=f"{ROLE_META[role].label} (desarrollo)",
                email_verified=True,
            )
            session.add(user)
            session.flush()
            session.add(UserRole(user_id=user.id, role=role))
            created += 1
            print(f"  created  {email}  [{role.value}]")
        if not dry_run:
            session.commit()
    finally:
        session.close()
    return created


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="show what would be created"
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    if settings.environment != "development":
        print(
            f"Refusing to seed: ENVIRONMENT is {settings.environment!r}, "
            "this script only runs in development.",
            file=sys.stderr,
        )
        return 1

    password = os.environ.get(PASSWORD_VARIABLE)
    if not password:
        print(
            f"Set {PASSWORD_VARIABLE} to the password these accounts should share.",
            file=sys.stderr,
        )
        return 1
    try:
        validate_password_policy(password)
    except ValueError as exc:
        print(f"{PASSWORD_VARIABLE} rejected: {exc}", file=sys.stderr)
        return 1

    print("Seeding development users, one per role:")
    created = seed(password, dry_run=args.dry_run)

    print(f"\n{created} user(s) created.")
    print(
        "\nWARNING: these are development credentials. They share one password, "
        "have no second factor, and must never exist in staging or production."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
