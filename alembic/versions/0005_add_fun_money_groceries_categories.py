"""add_fun_money_groceries_categories

Revision ID: 0005_add_fun_money_groceries_categories
Revises: 0004_add_transaction_date
Create Date: 2026-05-27

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0005_add_fun_money_groceries_categories"
down_revision: Union[str, None] = "0004_add_transaction_date"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO categories (name, display_name, is_transfer) "
        "VALUES ('fun_money', 'Fun Money', 0)"
    )
    op.execute(
        "INSERT INTO categories (name, display_name, is_transfer) "
        "VALUES ('groceries', 'Groceries', 0)"
    )


def downgrade() -> None:
    op.execute("DELETE FROM categories WHERE name IN ('fun_money', 'groceries')")