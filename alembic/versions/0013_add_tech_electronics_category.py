"""add_tech_electronics_category

Revision ID: 0013_add_tech_electronics_category
Revises: 0012_add_skipped_json
Create Date: 2026-06-01

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0013_add_tech_electronics_category"
down_revision: Union[str, None] = "0012_add_skipped_json"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO categories (name, display_name, is_transfer) "
        "VALUES ('tech_electronics', 'Tech / Electronics', 0)"
    )


def downgrade() -> None:
    op.execute("DELETE FROM categories WHERE name = 'tech_electronics'")
