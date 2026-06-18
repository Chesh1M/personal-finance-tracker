"""add_statement_balance

Revision ID: 0008_add_statement_balance
Revises: 0007_add_cash_withdrawal_category
Create Date: 2026-05-28

Adds closing_balance and account_type columns to statement_uploads so the app
can track per-account bank balances for net-worth display on the dashboard.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_add_statement_balance"
down_revision: Union[str, None] = "0007_add_cash_withdrawal_category"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("statement_uploads") as batch_op:
        batch_op.add_column(sa.Column("closing_balance", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("account_type",    sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("statement_uploads") as batch_op:
        batch_op.drop_column("closing_balance")
        batch_op.drop_column("account_type")
