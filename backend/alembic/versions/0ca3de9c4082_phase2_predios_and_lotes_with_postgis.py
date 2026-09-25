"""phase2: predios and lotes with postgis

Revision ID: 0ca3de9c4082
Revises: c0004f4adba1
Create Date: 2026-09-24

Hand-written: autogenerate handles neither PostGIS column types nor GiST and
partial indexes well. See docs/decisions/ADR-003-geospatial-model.md.
"""

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op

revision: str = "0ca3de9c4082"
down_revision: str | Sequence[str] | None = "c0004f4adba1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SRID = 4326

# PostGIS type modifiers cannot say "Polygon or MultiPolygon": the column is a
# generic geometry with the SRID pinned, and a CHECK pins the shape.
POLYGONAL = "ST_GeometryType(geometry) IN ('ST_Polygon', 'ST_MultiPolygon')"


def _area_geometry() -> geoalchemy2.Geometry:
    return geoalchemy2.Geometry(geometry_type="GEOMETRY", srid=SRID, spatial_index=False)


def _point() -> geoalchemy2.Geometry:
    return geoalchemy2.Geometry(geometry_type="POINT", srid=SRID, spatial_index=False)


def _audit_columns() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by_user_id", sa.Uuid(), nullable=True),
    ]


def upgrade() -> None:
    """Create predios and lotes, and make user_predio_roles point at predios."""
    # The schema below depends on PostGIS, so the migration says so itself
    # instead of relying on an init script having run first (a database created
    # by hand, or by the test suite, would otherwise lack the geometry type).
    # The downgrade leaves it installed: other objects may depend on it.
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    op.create_table(
        "predios",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("geometry", _area_geometry(), nullable=False),
        sa.Column("centroid", _point(), nullable=False),
        sa.Column("area_m2", sa.Float(), nullable=False),
        sa.Column("region", sa.String(length=120), nullable=True),
        sa.Column("comuna", sa.String(length=120), nullable=True),
        sa.Column("address", sa.String(length=255), nullable=True),
        sa.Column("rol_sii", sa.String(length=32), nullable=True),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["deleted_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("area_m2 > 0", name="ck_predios_area_positive"),
        sa.CheckConstraint(POLYGONAL, name="ck_predios_polygonal"),
    )
    op.create_index("ix_predios_owner_user_id", "predios", ["owner_user_id"])
    op.create_index("ix_predios_slug", "predios", ["slug"])
    op.create_index("ix_predios_geometry", "predios", ["geometry"], postgresql_using="gist")
    op.create_index("ix_predios_centroid", "predios", ["centroid"], postgresql_using="gist")
    op.create_index(
        "uq_predios_owner_slug_active",
        "predios",
        ["owner_user_id", "slug"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_predios_active",
        "predios",
        ["deleted_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "lotes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("predio_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=True),
        sa.Column(
            "lote_type",
            sa.Enum(
                "CUARTEL",
                "POTRERO",
                "PARCELA",
                "INVERNADERO",
                "BODEGA_AREA",
                "OTRO",
                name="lotetype",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("geometry", _area_geometry(), nullable=False),
        sa.Column("centroid", _point(), nullable=False),
        sa.Column("area_m2", sa.Float(), nullable=False),
        sa.Column("crop_type", sa.String(length=120), nullable=True),
        sa.Column("variety", sa.String(length=120), nullable=True),
        sa.Column("planting_year", sa.Integer(), nullable=True),
        sa.Column("plant_count", sa.Integer(), nullable=True),
        sa.Column("row_spacing_m", sa.Float(), nullable=True),
        sa.Column("plant_spacing_m", sa.Float(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["predio_id"], ["predios.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["deleted_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("area_m2 > 0", name="ck_lotes_area_positive"),
        sa.CheckConstraint(POLYGONAL, name="ck_lotes_polygonal"),
    )
    op.create_index("ix_lotes_predio_id", "lotes", ["predio_id"])
    op.create_index("ix_lotes_geometry", "lotes", ["geometry"], postgresql_using="gist")
    op.create_index(
        "uq_lotes_predio_name_active",
        "lotes",
        ["predio_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # Phase 1 left predio_id as a logical reference; it becomes a real FK now.
    # RESTRICT: a predio with people assigned can never be physically deleted.
    op.create_foreign_key(
        "fk_user_predio_roles_predio_id_predios",
        "user_predio_roles",
        "predios",
        ["predio_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # Explicit grants, on top of the default privileges set in phase 1: every
    # migration states what the runtime role may do with the tables it adds.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lar_app') THEN
                RAISE EXCEPTION
                    'Role lar_app is missing. Run scripts/init-db.sql first.';
            END IF;
        END
        $$;
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON predios, lotes TO lar_app")
    # UUID keys mean these tables own no sequences; kept so a future serial
    # column is covered without anyone having to remember.
    op.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO lar_app")


def downgrade() -> None:
    """Drop the geospatial tables and return predio_id to a logical reference."""
    op.drop_constraint(
        "fk_user_predio_roles_predio_id_predios", "user_predio_roles", type_="foreignkey"
    )
    op.drop_table("lotes")
    op.drop_table("predios")
