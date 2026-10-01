"""phase3: satellite analysis (analyses, per-lote stats, anomalies)

Revision ID: 7c3d1e9f5a20
Revises: 5b1e7c2d9a40
Create Date: 2026-10-01

Hand-written: autogenerate handles neither PostGIS column types nor GiST and
partial indexes well. Invariants are enforced here as check constraints so no
writer (service, worker, script) can skip them. See ADR-004.
"""

from collections.abc import Sequence

import geoalchemy2
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7c3d1e9f5a20"
down_revision: str | Sequence[str] | None = "5b1e7c2d9a40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SRID = 4326
TABLES = ("analyses", "analysis_lote_stats", "anomalies")

_STAT_COLUMNS = (
    "mean",
    "median",
    "std_dev",
    "min_value",
    "max_value",
    "percentile_10",
    "percentile_90",
)
_ALL_STATS_PRESENT = " AND ".join(f"{column} IS NOT NULL" for column in _STAT_COLUMNS)
_ALL_STATS_ABSENT = " AND ".join(f"{column} IS NULL" for column in _STAT_COLUMNS)


def _enum(name: str, *values: str, length: int) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, length=length)


def upgrade() -> None:
    """Create the three analysis tables and grant them to the runtime role."""
    op.create_table(
        "analyses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("predio_id", sa.Uuid(), nullable=False),
        sa.Column("analysis_type", _enum("analysistype", "NDVI", length=32), nullable=False),
        sa.Column(
            "status",
            _enum(
                "analysisstatus",
                "PENDING",
                "RUNNING",
                "COMPLETED",
                "FAILED",
                "NO_SUITABLE_SCENE",
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("requested_date_from", sa.Date(), nullable=False),
        sa.Column("requested_date_to", sa.Date(), nullable=False),
        sa.Column("scene_date", sa.Date(), nullable=True),
        sa.Column("scene_id", sa.String(length=512), nullable=True),
        sa.Column("cloud_coverage", sa.Float(), nullable=True),
        sa.Column("valid_pixels_ratio", sa.Float(), nullable=True),
        sa.Column("processing_units_spent", sa.Float(), nullable=True),
        sa.Column("raster_key", sa.String(length=512), nullable=True),
        sa.Column("preview_key", sa.String(length=512), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("force", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("superseded_by_analysis_id", sa.Uuid(), nullable=True),
        sa.Column("resolved_to_analysis_id", sa.Uuid(), nullable=True),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["predio_id"], ["predios.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["superseded_by_analysis_id"], ["analyses.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["resolved_to_analysis_id"], ["analyses.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "requested_date_from <= requested_date_to",
            name="ck_analyses_date_range_ordered",
        ),
        sa.CheckConstraint(
            "cloud_coverage IS NULL OR (cloud_coverage >= 0 AND cloud_coverage <= 1)",
            name="ck_analyses_cloud_coverage_ratio",
        ),
        sa.CheckConstraint(
            "valid_pixels_ratio IS NULL "
            "OR (valid_pixels_ratio >= 0 AND valid_pixels_ratio <= 1)",
            name="ck_analyses_valid_pixels_ratio",
        ),
        sa.CheckConstraint(
            "processing_units_spent IS NULL OR processing_units_spent >= 0",
            name="ck_analyses_pu_non_negative",
        ),
        sa.CheckConstraint(
            "status <> 'COMPLETED' OR (scene_id IS NOT NULL "
            "AND scene_date IS NOT NULL AND completed_at IS NOT NULL)",
            name="ck_analyses_completed_has_scene",
        ),
        sa.CheckConstraint(
            "status <> 'FAILED' OR error_code IS NOT NULL",
            name="ck_analyses_failed_has_error_code",
        ),
        sa.CheckConstraint(
            "(status = 'DUPLICATE_SCENE') = (resolved_to_analysis_id IS NOT NULL)",
            name="ck_analyses_duplicate_resolved",
        ),
        sa.CheckConstraint(
            "superseded_by_analysis_id IS NULL OR status = 'COMPLETED'",
            name="ck_analyses_superseded_only_completed",
        ),
    )
    op.create_index("ix_analyses_predio_id", "analyses", ["predio_id"])
    op.create_index("ix_analyses_status", "analyses", ["status"])
    op.create_index("ix_analyses_scene_date", "analyses", ["scene_date"])
    # Idempotency: one current result per (predio, type, scene). A forced
    # recompute supersedes the previous one instead of deleting it.
    op.create_index(
        "uq_analyses_completed_scene",
        "analyses",
        ["predio_id", "analysis_type", "scene_id"],
        unique=True,
        postgresql_where=sa.text(
            "status = 'COMPLETED' AND superseded_by_analysis_id IS NULL"
        ),
    )
    # At most one analysis of a type in flight per predio.
    op.create_index(
        "uq_analyses_one_active_per_type",
        "analyses",
        ["predio_id", "analysis_type"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING', 'RUNNING')"),
    )

    op.create_table(
        "analysis_lote_stats",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("analysis_id", sa.Uuid(), nullable=False),
        sa.Column("lote_id", sa.Uuid(), nullable=False),
        *(sa.Column(column, sa.Float(), nullable=True) for column in _STAT_COLUMNS),
        sa.Column("valid_pixels", sa.Integer(), nullable=False),
        sa.Column("total_pixels", sa.Integer(), nullable=False),
        sa.Column(
            "excluded_reason",
            _enum(
                "lotestatsexclusion",
                "NOT_APPLICABLE_GREENHOUSE",
                "INSUFFICIENT_PIXELS",
                length=48,
            ),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lote_id"], ["lotes.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("analysis_id", "lote_id", name="uq_analysis_lote_stats_lote"),
        sa.CheckConstraint(
            "valid_pixels >= 0 AND total_pixels >= 0 AND valid_pixels <= total_pixels",
            name="ck_analysis_lote_stats_pixel_counts",
        ),
        sa.CheckConstraint(
            f"(excluded_reason IS NULL AND {_ALL_STATS_PRESENT}) "
            f"OR (excluded_reason IS NOT NULL AND {_ALL_STATS_ABSENT})",
            name="ck_analysis_lote_stats_measured_or_excluded",
        ),
    )
    op.create_index(
        "ix_analysis_lote_stats_analysis_id", "analysis_lote_stats", ["analysis_id"]
    )
    op.create_index("ix_analysis_lote_stats_lote_id", "analysis_lote_stats", ["lote_id"])

    op.create_table(
        "anomalies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("analysis_id", sa.Uuid(), nullable=False),
        sa.Column("lote_id", sa.Uuid(), nullable=True),
        sa.Column(
            "geometry",
            geoalchemy2.Geometry(geometry_type="POLYGON", srid=SRID, spatial_index=False),
            nullable=False,
        ),
        sa.Column(
            "centroid",
            geoalchemy2.Geometry(geometry_type="POINT", srid=SRID, spatial_index=False),
            nullable=False,
        ),
        sa.Column("area_m2", sa.Float(), nullable=False),
        sa.Column("area_ratio_of_lote", sa.Float(), nullable=False),
        sa.Column(
            "severity", _enum("anomalyseverity", "LOW", "MEDIUM", "HIGH", length=16), nullable=False
        ),
        sa.Column("mean_zscore", sa.Float(), nullable=False),
        sa.Column("mean_index_value", sa.Float(), nullable=False),
        sa.Column("pixel_count", sa.Integer(), nullable=False),
        sa.Column("reviewed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "review_status",
            _enum(
                "anomalyreviewstatus",
                "CONFIRMED",
                "DISMISSED",
                "NEEDS_FIELD_CHECK",
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["analysis_id"], ["analyses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lote_id"], ["lotes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("area_m2 > 0", name="ck_anomalies_area_positive"),
        sa.CheckConstraint(
            "area_ratio_of_lote > 0 AND area_ratio_of_lote <= 1",
            name="ck_anomalies_area_ratio",
        ),
        sa.CheckConstraint("pixel_count > 0", name="ck_anomalies_pixel_count_positive"),
        sa.CheckConstraint("mean_zscore < 0", name="ck_anomalies_zscore_negative"),
        sa.CheckConstraint(
            "(review_status IS NULL) = (reviewed_at IS NULL) "
            "AND (review_status IS NULL) = (reviewed_by_user_id IS NULL)",
            name="ck_anomalies_review_complete",
        ),
    )
    op.create_index("ix_anomalies_analysis_id", "anomalies", ["analysis_id"])
    op.create_index("ix_anomalies_lote_id", "anomalies", ["lote_id"])
    op.create_index(
        "ix_anomalies_geometry", "anomalies", ["geometry"], postgresql_using="gist"
    )

    # Explicit grants on top of the phase 1 default privileges: every
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
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(TABLES)} TO lar_app")


def downgrade() -> None:
    """Drop the analysis tables, children first."""
    op.drop_table("anomalies")
    op.drop_table("analysis_lote_stats")
    op.drop_table("analyses")
