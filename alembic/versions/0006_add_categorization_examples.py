"""add_categorization_examples

Revision ID: 0006_add_categorization_examples
Revises: 0005_add_fun_money_groceries_categories
Create Date: 2026-05-27

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_add_categorization_examples"
down_revision: Union[str, None] = "0005_add_fun_money_groceries_categories"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "categorization_examples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("description", sa.String(), unique=True, nullable=False, index=True),
        sa.Column("category_id", sa.Integer(), sa.ForeignKey("categories.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("categorization_examples")