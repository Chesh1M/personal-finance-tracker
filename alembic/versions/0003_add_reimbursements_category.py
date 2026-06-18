"""add_reimbursements_category

Revision ID: 0003_add_reimbursements_category
Revises: 0002_seed_default_categories
Create Date: 2026-05-26

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003_add_reimbursements_category"
down_revision: Union[str, None] = "0002_seed_default_categories"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO categories (name, display_name, is_transfer) "
        "VALUES ('reimbursements', 'Reimbursements', false)"
    )


def downgrade() -> None:
    op.execute("DELETE FROM categories WHERE name = 'reimbursements'")
