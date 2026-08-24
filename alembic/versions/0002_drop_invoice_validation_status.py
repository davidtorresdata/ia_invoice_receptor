"""drop invoices.validation_status (field retired: no current value)

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_invoices_validation_status", table_name="invoices")
    op.drop_column("invoices", "validation_status")


def downgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column("validation_status", sa.String(length=16), nullable=False,
                  server_default="PENDING"),
    )
    op.create_index(
        "ix_invoices_validation_status", "invoices", ["validation_status"]
    )
