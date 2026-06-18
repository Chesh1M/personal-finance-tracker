"""add_cash_withdrawal_category

Revision ID: 0007_add_cash_withdrawal_category
Revises: 0006_add_categorization_examples
Create Date: 2026-05-27

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0007_add_cash_withdrawal_category"
down_revision: Union[str, None] = "0006_add_categorization_examples"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO categories (name, display_name, is_transfer) "
        "VALUES ('cash_withdrawal', 'Cash Withdrawal', false)"
    )


def downgrade() -> None:
    op.execute("DELETE FROM categories WHERE name = 'cash_withdrawal'")