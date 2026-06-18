"""add_skipped_json

Revision ID: 0012_add_skipped_json
Revises: 0011_add_motorbike_maintenance_category
Create Date: 2026-06-01

Stores JSON list of transactions that were skipped as duplicates during upload,
so the user can inspect them from the upload page.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_add_skipped_json"
down_revision: Union[str, None] = "0011_add_motorbike_maintenance_category"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("statement_uploads") as batch_op:
        batch_op.add_column(sa.Column("skipped_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("statement_uploads") as batch_op:
        batch_op.drop_column("skipped_json")
