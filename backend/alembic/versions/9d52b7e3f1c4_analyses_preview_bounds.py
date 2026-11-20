"""analyses.preview_*: the bounds of the preview, for a map overlay

Revision ID: 9d52b7e3f1c4
Revises: 8a41c6d2e7b3
Create Date: 2026-11-20

preview.png is not georeferenced. A map (Leaflet imageOverlay) needs its
corners, which are those of ndvi.tif (same grid). They are stored once when
the analysis completes, so the API never opens a raster to answer a GET.

Four float columns rather than a JSONB: each one is typed, and a CHECK can
require the box to be complete, inside EPSG:4326 and not flipped, which a JSON
document would only get from code that every writer has to remember to call.
The complete branch repeats IS NOT NULL on purpose: a comparison with NULL is
NULL, and a CHECK that evaluates to NULL passes, so without it a box with one
corner missing would be accepted.

Analyses completed before this migration keep NULL bounds: the migration has
no access to the raster storage to backfill them.

Adding columns does not change table privileges: no GRANT is needed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9d52b7e3f1c4"
down_revision: str | Sequence[str] | None = "8a41c6d2e7b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = ("preview_west", "preview_south", "preview_east", "preview_north")

# Same text as app.modules.analysis.models.PREVIEW_BOUNDS_CHECK.
PREVIEW_BOUNDS_CHECK = (
    "(preview_west IS NULL AND preview_south IS NULL "
    "AND preview_east IS NULL AND preview_north IS NULL) "
    "OR (preview_west IS NOT NULL AND preview_south IS NOT NULL "
    "AND preview_east IS NOT NULL AND preview_north IS NOT NULL "
    "AND preview_west >= -180 AND preview_east <= 180 AND preview_west < preview_east "
    "AND preview_south >= -90 AND preview_north <= 90 "
    "AND preview_south < preview_north)"
)


def upgrade() -> None:
    for column in COLUMNS:
        op.add_column("analyses", sa.Column(column, sa.Float(), nullable=True))
    op.create_check_constraint(
        "ck_analyses_preview_bounds", "analyses", PREVIEW_BOUNDS_CHECK
    )


def downgrade() -> None:
    op.drop_constraint("ck_analyses_preview_bounds", "analyses", type_="check")
    for column in reversed(COLUMNS):
        op.drop_column("analyses", column)
