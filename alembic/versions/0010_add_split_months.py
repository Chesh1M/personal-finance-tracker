"""add_split_months

Revision ID: 0010_add_split_months
Revises: 0009_add_reimb_category_tag
Create Date: 2026-05-28

Adds split_start_month and split_end_month (YYYY-MM strings) to transactions
so a reimbursement can be spread equally across a date range rather than
applied in full to the month it was received.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_add_split_months"
down_revision: Union[str, None] = "0009_add_reimb_category_tag"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.add_column(sa.Column("split_start_month", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("split_end_month",   sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_column("split_end_month")
        batch_op.drop_column("split_start_month")
