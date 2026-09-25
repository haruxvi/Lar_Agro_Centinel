"""phase2: geometry repair traceability on predios and lotes

Revision ID: 5b1e7c2d9a40
Revises: 0ca3de9c4082
Create Date: 2026-09-24

Records whether a stored boundary was automatically repaired and how much the
repair changed its area. Both tables already exist and are granted to lar_app,
so no GRANT is needed: adding columns does not change table privileges.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5b1e7c2d9a40"
down_revision: str | Sequence[str] | None = "0ca3de9c4082"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("predios", "lotes")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(
            table,
            sa.Column(
                "geometry_was_repaired",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            ),
        )
        op.add_column(
            table,
            sa.Column("geometry_repair_area_delta_m2", sa.Float(), nullable=True),
        )
        op.create_check_constraint(
            f"ck_{table}_repair_delta_requires_repair",
            table,
            "geometry_was_repaired OR geometry_repair_area_delta_m2 IS NULL",
        )


def downgrade() -> None:
    for table in TABLES:
        op.drop_constraint(f"ck_{table}_repair_delta_requires_repair", table, type_="check")
        op.drop_column(table, "geometry_repair_area_delta_m2")
        op.drop_column(table, "geometry_was_repaired")
