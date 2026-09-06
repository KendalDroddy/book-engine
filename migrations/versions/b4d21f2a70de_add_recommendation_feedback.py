"""add recommendation feedback

Revision ID: b4d21f2a70de
Revises: 8d8c6e8c4f1a
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4d21f2a70de"
down_revision: str | None = "8d8c6e8c4f1a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recommendation_feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("recommendation_item_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("context_json", sa.JSON(), nullable=False),
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
            "action IN ('add_to_want_to_read', 'not_interested', "
            "'loved', 'liked', 'fine', 'miss')",
            name=op.f("ck_recommendation_feedback_action"),
        ),
        sa.ForeignKeyConstraint(
            ["recommendation_item_id"],
            ["recommendation_items.id"],
            name=op.f(
                "fk_recommendation_feedback_recommendation_item_id_recommendation_items"
            ),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["work_id"],
            ["works.id"],
            name=op.f("fk_recommendation_feedback_work_id_works"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recommendation_feedback")),
        sa.UniqueConstraint(
            "work_id",
            "action",
            name=op.f("uq_recommendation_feedback_work_id"),
        ),
    )


def downgrade() -> None:
    op.drop_table("recommendation_feedback")
