"""add_ai_monthly_insights

Revision ID: 0015_add_ai_monthly_insights
Revises: 0014_add_users_and_user_id
Create Date: 2026-08-11

Adds `ai_monthly_insights` table to cache per-user per-month GPT spending
insight text so the dashboard doesn't call the API on every page load.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_add_ai_monthly_insights"
down_revision: Union[str, None] = "0014_add_users_and_user_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_monthly_insights",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("year", sa.Integer, nullable=False),
        sa.Column("month", sa.Integer, nullable=False),
        sa.Column("insight_text", sa.Text, nullable=True),
        sa.Column("generated_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("user_id", "year", "month", name="uq_ai_insight_user_year_month"),
    )


def downgrade() -> None:
    op.drop_table("ai_monthly_insights")
