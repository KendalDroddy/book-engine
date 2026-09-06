"""add recommendation display eligibility

Revision ID: cc73b30e618a
Revises: b4d21f2a70de
Create Date: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cc73b30e618a"
down_revision: str | Sequence[str] | None = "b4d21f2a70de"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("recommendation_items") as batch_op:
        batch_op.add_column(
            sa.Column(
                "display_eligible",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        batch_op.add_column(sa.Column("eligible_rank", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "eligibility_reasons", sa.JSON(), nullable=False, server_default="[]"
            )
        )
        batch_op.add_column(
            sa.Column(
                "eligibility_warnings", sa.JSON(), nullable=False, server_default="[]"
            )
        )
        batch_op.add_column(
            sa.Column(
                "eligibility_provenance", sa.JSON(), nullable=False, server_default="{}"
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("recommendation_items") as batch_op:
        batch_op.drop_column("eligibility_provenance")
        batch_op.drop_column("eligibility_warnings")
        batch_op.drop_column("eligibility_reasons")
        batch_op.drop_column("eligible_rank")
        batch_op.drop_column("display_eligible")
