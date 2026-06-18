"""seed_default_categories

Revision ID: 0002_seed_default_categories
Revises: 19cf960c1918
Create Date: 2026-05-26

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002_seed_default_categories"
down_revision: Union[str, None] = "19cf960c1918"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CATEGORIES = [
    {"name": "food_dining",     "display_name": "Food & Dining",          "is_transfer": False},
    {"name": "transport",       "display_name": "Transport",               "is_transfer": False},
    {"name": "shopping",        "display_name": "Shopping",                "is_transfer": False},
    {"name": "entertainment",   "display_name": "Entertainment",           "is_transfer": False},
    {"name": "utilities_bills", "display_name": "Utilities & Bills",       "is_transfer": False},
    {"name": "healthcare",      "display_name": "Healthcare",              "is_transfer": False},
    {"name": "travel",          "display_name": "Travel",                  "is_transfer": False},
    {"name": "education",       "display_name": "Education",               "is_transfer": False},
    {"name": "personal_care",   "display_name": "Personal Care",           "is_transfer": False},
    {"name": "subscriptions",   "display_name": "Subscriptions",           "is_transfer": False},
    {"name": "transfers",       "display_name": "Transfers",               "is_transfer": True},
    {"name": "income",          "display_name": "Income",                  "is_transfer": False},
    {"name": "others",          "display_name": "Others / Uncategorized",  "is_transfer": False},
]


def upgrade() -> None:
    categories_table = sa.table(
        "categories",
        sa.column("name", sa.String),
        sa.column("display_name", sa.String),
        sa.column("is_transfer", sa.Boolean),
    )
    op.bulk_insert(categories_table, CATEGORIES)


def downgrade() -> None:
    op.execute(
        "DELETE FROM categories WHERE name IN ({})".format(
            ", ".join(f"'{c['name']}'" for c in CATEGORIES)
        )
    )
