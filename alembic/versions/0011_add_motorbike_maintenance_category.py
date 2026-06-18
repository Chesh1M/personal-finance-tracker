"""add_motorbike_maintenance_category

Revision ID: 0011_add_motorbike_maintenance_category
Revises: 0010_add_split_months
Create Date: 2026-05-31

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0011_add_motorbike_maintenance_category"
down_revision: Union[str, None] = "0010_add_split_months"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO categories (name, display_name, is_transfer) "
        "VALUES ('motorbike_maintenance', 'Motorbike Maintenance', 0)"
    )


def downgrade() -> None:
    op.execute("DELETE FROM categories WHERE name = 'motorbike_maintenance'")
