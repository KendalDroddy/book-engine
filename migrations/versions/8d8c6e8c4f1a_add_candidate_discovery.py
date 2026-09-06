"""add candidate discovery

Revision ID: 8d8c6e8c4f1a
Revises: f0b0a91148f5
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8d8c6e8c4f1a"
down_revision: str | None = "f0b0a91148f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "discovery_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("strategy_version", sa.String(length=50), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("requested_limit", sa.Integer(), nullable=False),
        sa.Column("selected_count", sa.Integer(), nullable=False),
        sa.Column("excluded_count", sa.Integer(), nullable=False),
        sa.Column("external_request_count", sa.Integer(), nullable=False),
        sa.Column("cache_hit", sa.Boolean(), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'completed_with_errors', 'failed')",
            name=op.f("ck_discovery_runs_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovery_runs")),
        sa.UniqueConstraint(
            "provider",
            "strategy_version",
            "input_hash",
            name=op.f("uq_discovery_runs_provider"),
        ),
    )
    op.create_table(
        "discovery_queries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("cluster_slug", sa.String(length=100), nullable=False),
        sa.Column("query_text", sa.String(length=1000), nullable=False),
        sa.Column("request_key", sa.String(length=1000), nullable=False),
        sa.Column("endpoint", sa.String(length=500), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=True),
        sa.Column("payload_checksum", sa.String(length=64), nullable=True),
        sa.Column("parser_version", sa.String(length=50), nullable=False),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["discovery_runs.id"],
            name=op.f("fk_discovery_queries_run_id_discovery_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovery_queries")),
        sa.UniqueConstraint(
            "run_id", "cluster_slug", name=op.f("uq_discovery_queries_run_id")
        ),
    )
    op.create_table(
        "discovery_candidates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("external_work_id", sa.String(length=300), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("primary_author", sa.String(length=300), nullable=False),
        sa.Column("discovery_rank", sa.Integer(), nullable=False),
        sa.Column("cluster_slugs", sa.JSON(), nullable=False),
        sa.Column("query_ids", sa.JSON(), nullable=False),
        sa.Column("identifiers", sa.JSON(), nullable=False),
        sa.Column("source_metadata", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("enrichment_attempt_id", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('selected', 'enriched', 'enrichment_failed')",
            name=op.f("ck_discovery_candidates_status"),
        ),
        sa.ForeignKeyConstraint(
            ["enrichment_attempt_id"],
            ["enrichment_attempts.id"],
            name=op.f(
                "fk_discovery_candidates_enrichment_attempt_id_enrichment_attempts"
            ),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["discovery_runs.id"],
            name=op.f("fk_discovery_candidates_run_id_discovery_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["work_id"],
            ["works.id"],
            name=op.f("fk_discovery_candidates_work_id_works"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovery_candidates")),
        sa.UniqueConstraint(
            "run_id",
            "provider",
            "external_work_id",
            name=op.f("uq_discovery_candidates_run_id"),
        ),
        sa.UniqueConstraint(
            "run_id", "work_id", name=op.f("uq_discovery_candidates_run_id_work_id")
        ),
    )


def downgrade() -> None:
    op.drop_table("discovery_candidates")
    op.drop_table("discovery_queries")
    op.drop_table("discovery_runs")
