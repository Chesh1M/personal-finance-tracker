from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    google_id = Column(String, unique=True, nullable=False, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    display_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_login_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Category(Base):
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)
    display_name = Column(String, nullable=False)
    is_transfer = Column(Boolean, default=False, nullable=False)

    transactions = relationship("Transaction", foreign_keys="[Transaction.category_id]", back_populates="category")


class StatementUpload(Base):
    __tablename__ = "statement_uploads"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    filename = Column(String, nullable=False)
    bank_source = Column(String, nullable=False)
    upload_date = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    raw_text = Column(Text)
    status = Column(String, default="pending", nullable=False)
    closing_balance = Column(Float, nullable=True)   # end-of-period balance; None = not extracted yet
    account_type = Column(String, nullable=True)     # "savings" | "current" | "credit_card" | "paylah" | "other"
    skipped_json = Column(Text, nullable=True)       # JSON list of transactions skipped as duplicates during upload

    transactions = relationship("Transaction", back_populates="statement")


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    statement_id = Column(Integer, ForeignKey("statement_uploads.id"), nullable=True)
    date = Column(Date, nullable=False)
    description = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    type = Column(String, nullable=False)          # "debit" or "credit"
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=True)
    transaction_date = Column(Date, nullable=True)  # actual spend date when visible in description; falls back to date (posting date) for analytics
    is_transfer = Column(Boolean, default=False, nullable=False)
    account_type = Column(String, nullable=True)   # e.g. "savings", "credit_card"
    is_reviewed = Column(Boolean, default=False, nullable=False)
    hash = Column(String, nullable=False, index=True)
    reimbursement_category_id = Column(Integer, ForeignKey("categories.id"), nullable=True)
    split_start_month = Column(String, nullable=True)   # "YYYY-MM" — start of split range
    split_end_month   = Column(String, nullable=True)   # "YYYY-MM" — end of split range

    __table_args__ = (
        UniqueConstraint("hash", "user_id", name="uq_transaction_hash_user"),
    )

    statement = relationship("StatementUpload", back_populates="transactions")
    category = relationship("Category", foreign_keys=[category_id], back_populates="transactions")


class CategorizationExample(Base):
    __tablename__ = "categorization_examples"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    description = Column(String, nullable=False, index=True)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    __table_args__ = (
        UniqueConstraint("description", "user_id", name="uq_categex_desc_user"),
    )

    category = relationship("Category")


class PortfolioPosition(Base):
    __tablename__ = "portfolio_positions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    ticker = Column(String, nullable=False, index=True)
    description = Column(String, nullable=True)
    quantity = Column(Float, nullable=False)
    avg_cost_price = Column(Float, nullable=False)
    cost_basis = Column(Float, nullable=False)
    currency = Column(String, default="USD", nullable=False)
    last_synced_date = Column(Date, nullable=True)


class TradeLog(Base):
    __tablename__ = "trade_log"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    ticker = Column(String, nullable=False, index=True)
    trade_type = Column(String, nullable=False)    # "BUY" or "SELL"
    quantity = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    date = Column(Date, nullable=False)
    currency = Column(String, default="USD", nullable=False)
    notes = Column(Text, nullable=True)
