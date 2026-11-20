"""analyses.requested_at records the moment of the request, not of the transaction

Revision ID: 8a41c6d2e7b3
Revises: 7c3d1e9f5a20
Create Date: 2026-11-20

``now()`` is the start of the transaction. Analyses created together in one
transaction (a scheduled job requesting them for several predios, a test)
would all share one requested_at, and "newest first" would depend on the
random UUID that breaks the tie. ``clock_timestamp()`` is the real instant of
each INSERT.

audit_log keeps ``now()`` on purpose: see KL-003, reviewed in Phase 7 with the
hash chain. Only the default changes; existing rows are left as they are.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8a41c6d2e7b3"
down_revision: str | Sequence[str] | None = "7c3d1e9f5a20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "analyses", "requested_at", server_default=sa.text("clock_timestamp()")
    )


def downgrade() -> None:
    op.alter_column("analyses", "requested_at", server_default=sa.text("now()"))
