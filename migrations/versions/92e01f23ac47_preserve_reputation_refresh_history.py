"""Allow multiple reputation attempts and observations without replacing history."""

import sqlalchemy as sa
from alembic import op

revision = "92e01f23ac47"
down_revision = "5ab3d72ac819"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("reputation_fetches") as batch:
        batch.drop_constraint("uq_reputation_fetches_provider", type_="unique")
        batch.create_index(
            "ix_reputation_fetches_provider_input_hash", ["provider", "input_hash"]
        )
    with op.batch_alter_table("reputation_observations") as batch:
        batch.drop_constraint("uq_reputation_observations_provider", type_="unique")


def downgrade() -> None:
    # The old schema cannot hold refresh history; never discard it on downgrade.
    connection = op.get_bind()
    for table, columns in (
        ("reputation_fetches", "provider, input_hash"),
        ("reputation_observations", "provider, provider_book_id, model_version"),
    ):
        duplicates = connection.execute(
            sa.text(f"SELECT 1 FROM {table} GROUP BY {columns} HAVING COUNT(*) > 1")
        ).first()
        if duplicates:
            raise RuntimeError("Cannot downgrade without losing reputation history")
    with op.batch_alter_table("reputation_observations") as batch:
        batch.create_unique_constraint(
            "uq_reputation_observations_provider",
            ["provider", "provider_book_id", "model_version"],
        )
    with op.batch_alter_table("reputation_fetches") as batch:
        batch.drop_index("ix_reputation_fetches_provider_input_hash")
        batch.create_unique_constraint(
            "uq_reputation_fetches_provider", ["provider", "input_hash"]
        )
