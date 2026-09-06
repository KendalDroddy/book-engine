"""add book reputation

Revision ID: 5ab3d72ac819
Revises: cc73b30e618a
Create Date: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5ab3d72ac819"
down_revision: str | Sequence[str] | None = "cc73b30e618a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reputation_fetches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("request_key", sa.String(length=1000), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("endpoint", sa.String(length=500), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("match_method", sa.String(length=100), nullable=True),
        sa.Column("match_confidence", sa.Float(), nullable=True),
        sa.Column("provider_book_id", sa.String(length=300), nullable=True),
        sa.Column("raw_response", sa.JSON(), nullable=True),
        sa.Column("decision_provenance", sa.JSON(), nullable=False),
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
            "status IN ('succeeded', 'missed', 'ambiguous', 'failed')",
            name=op.f("ck_reputation_fetches_status"),
        ),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "input_hash"),
    )
    op.create_table(
        "reputation_observations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("fetch_id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("provider_book_id", sa.String(length=300), nullable=False),
        sa.Column("average_rating", sa.Float(), nullable=False),
        sa.Column("ratings_count", sa.Integer(), nullable=False),
        sa.Column("normalized_score", sa.Float(), nullable=False),
        sa.Column("reputation_confidence", sa.Float(), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("model_version", sa.String(length=100), nullable=False),
        sa.Column("model_provenance", sa.JSON(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
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
            ["fetch_id"], ["reputation_fetches.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fetch_id"),
        sa.UniqueConstraint("provider", "provider_book_id", "model_version"),
    )
    with op.batch_alter_table("recommendation_items") as batch_op:
        batch_op.add_column(
            sa.Column("reputation_observation_id", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "reputation_adjustment", sa.Float(), nullable=False, server_default="0"
            )
        )
        batch_op.add_column(sa.Column("combined_score", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("combined_rank", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            op.f(
                "fk_recommendation_items_reputation_observation_id_reputation_observations"
            ),
            "reputation_observations",
            ["reputation_observation_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("recommendation_items") as batch_op:
        batch_op.drop_constraint(
            op.f(
                "fk_recommendation_items_reputation_observation_id_reputation_observations"
            ),
            type_="foreignkey",
        )
        batch_op.drop_column("combined_rank")
        batch_op.drop_column("combined_score")
        batch_op.drop_column("reputation_adjustment")
        batch_op.drop_column("reputation_observation_id")
    op.drop_table("reputation_observations")
    op.drop_table("reputation_fetches")
