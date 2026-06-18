"""add_transaction_date

Revision ID: 0004_add_transaction_date
Revises: 0003_add_reimbursements_category
Create Date: 2026-05-27

Adds a nullable transaction_date column to transactions.
This stores the actual spend date when it differs from the posting date
(e.g. a charge on 26 Feb that appears on a 28 Feb statement line).
Analytics should use transaction_date when set, falling back to date.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004_add_transaction_date"
down_revision: Union[str, None] = "0003_add_reimbursements_category"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "transactions",
        sa.Column("transaction_date", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("transactions", "transaction_date")
