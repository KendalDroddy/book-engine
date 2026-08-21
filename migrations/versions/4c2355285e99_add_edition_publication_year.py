"""add edition publication year

Revision ID: 4c2355285e99
Revises: 01ddce26335e
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4c2355285e99"
down_revision: str | Sequence[str] | None = "01ddce26335e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("editions") as batch_op:
        batch_op.add_column(sa.Column("publication_year", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("editions") as batch_op:
        batch_op.drop_column("publication_year")
