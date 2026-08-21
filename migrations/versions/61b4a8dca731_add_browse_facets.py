"""add browse facets

Revision ID: 61b4a8dca731
Revises: 89793769d1c7
Create Date: 2026-08-21 15:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "61b4a8dca731"
down_revision: str | Sequence[str] | None = "89793769d1c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "browse_facets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_browse_facets")),
        sa.UniqueConstraint("category", "label", name=op.f("uq_browse_facets_category")),
        sa.UniqueConstraint("slug", name=op.f("uq_browse_facets_slug")),
    )
    op.create_table(
        "concept_facet_mappings",
        sa.Column("concept_id", sa.Integer(), nullable=False),
        sa.Column("facet_id", sa.Integer(), nullable=False),
        sa.Column("mapping_rule", sa.String(length=100), nullable=False),
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
            ["concept_id"],
            ["concepts.id"],
            name=op.f("fk_concept_facet_mappings_concept_id_concepts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["facet_id"],
            ["browse_facets.id"],
            name=op.f("fk_concept_facet_mappings_facet_id_browse_facets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "concept_id", "facet_id", name=op.f("pk_concept_facet_mappings")
        ),
    )


def downgrade() -> None:
    op.drop_table("concept_facet_mappings")
    op.drop_table("browse_facets")
