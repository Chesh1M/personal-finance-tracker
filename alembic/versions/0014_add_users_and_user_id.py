"""add_users_and_user_id

Revision ID: 0014_add_users_and_user_id
Revises: 0013_add_tech_electronics_category
Create Date: 2026-06-18

Adds multi-user support:
- Creates the `users` table
- Adds nullable `user_id` FK to statement_uploads, transactions,
  categorization_examples, portfolio_positions, trade_log
- Drops unique index on transactions.hash; adds composite UNIQUE(hash, user_id)
- Drops unique index on categorization_examples.description; adds composite
  UNIQUE(description, user_id)
- Data migration: inserts a seed 'local_dev' user and backfills all existing
  rows to user_id = 1
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_add_users_and_user_id"
down_revision: Union[str, None] = "0013_add_tech_electronics_category"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Create users table ────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("google_id", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("google_id"),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_users_id", "users", ["id"])
    op.create_index("ix_users_google_id", "users", ["google_id"])
    op.create_index("ix_users_email", "users", ["email"])

    # ── Seed local dev user (id=1) ────────────────────────────────────────
    op.execute(
        "INSERT INTO users (google_id, email, display_name, created_at, last_login_at) "
        "VALUES ('local_dev', 'local@dev', 'Local Dev', NOW(), NOW())"
    )

    # ── statement_uploads: add user_id ────────────────────────────────────
    with op.batch_alter_table("statement_uploads") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_su_user_id", "users", ["user_id"], ["id"])
    op.execute("UPDATE statement_uploads SET user_id = 1")

    # ── transactions: drop global hash unique index; add user_id + composite
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_tx_user_id", "users", ["user_id"], ["id"])
        batch_op.drop_index("ix_transactions_hash")
        batch_op.create_unique_constraint("uq_transaction_hash_user", ["hash", "user_id"])
    op.execute("UPDATE transactions SET user_id = 1")

    # ── categorization_examples: drop global desc unique; add user_id + composite
    with op.batch_alter_table("categorization_examples") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_ce_user_id", "users", ["user_id"], ["id"])
        batch_op.drop_index("ix_categorization_examples_description")
        batch_op.create_unique_constraint("uq_categex_desc_user", ["description", "user_id"])
    op.execute("UPDATE categorization_examples SET user_id = 1")

    # ── portfolio_positions: add user_id ──────────────────────────────────
    with op.batch_alter_table("portfolio_positions") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_pp_user_id", "users", ["user_id"], ["id"])
    op.execute("UPDATE portfolio_positions SET user_id = 1")

    # ── trade_log: add user_id ────────────────────────────────────────────
    with op.batch_alter_table("trade_log") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_tl_user_id", "users", ["user_id"], ["id"])
    op.execute("UPDATE trade_log SET user_id = 1")


def downgrade() -> None:
    with op.batch_alter_table("trade_log") as batch_op:
        batch_op.drop_constraint("fk_tl_user_id", type_="foreignkey")
        batch_op.drop_column("user_id")

    with op.batch_alter_table("portfolio_positions") as batch_op:
        batch_op.drop_constraint("fk_pp_user_id", type_="foreignkey")
        batch_op.drop_column("user_id")

    with op.batch_alter_table("categorization_examples") as batch_op:
        batch_op.drop_constraint("uq_categex_desc_user", type_="unique")
        batch_op.create_index("ix_categorization_examples_description", ["description"], unique=True)
        batch_op.drop_constraint("fk_ce_user_id", type_="foreignkey")
        batch_op.drop_column("user_id")

    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_constraint("uq_transaction_hash_user", type_="unique")
        batch_op.create_index("ix_transactions_hash", ["hash"], unique=True)
        batch_op.drop_constraint("fk_tx_user_id", type_="foreignkey")
        batch_op.drop_column("user_id")

    with op.batch_alter_table("statement_uploads") as batch_op:
        batch_op.drop_constraint("fk_su_user_id", type_="foreignkey")
        batch_op.drop_column("user_id")

    op.drop_index("ix_users_email", "users")
    op.drop_index("ix_users_google_id", "users")
    op.drop_index("ix_users_id", "users")
    op.drop_table("users")
