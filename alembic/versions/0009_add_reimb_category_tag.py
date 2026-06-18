"""add_reimb_category_tag

Revision ID: 0009_add_reimb_category_tag
Revises: 0008_add_statement_balance
Create Date: 2026-05-28

Adds reimbursement_category_id to transactions so users can tag a reimbursement
to the spending category it offsets (e.g. a dinner refund → Food & Dining).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_add_reimb_category_tag"
down_revision: Union[str, None] = "0008_add_statement_balance"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.add_column(
            sa.Column("reimbursement_category_id", sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_column("reimbursement_category_id")
