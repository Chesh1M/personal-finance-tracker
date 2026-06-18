"""Dedup tests: verify transaction hash uniqueness logic."""
import pytest
from datetime import date
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Category, StatementUpload, Transaction, User
from app.services.pdf_parser import compute_transaction_hash

_TEST_DATE = date(2026, 1, 1)
_TEST_DATE_STR = "2026-01-01"


@pytest.fixture()
def db():
    """In-memory SQLite DB with all tables created."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    # Seed a user and a statement
    user = User(google_id="test", email="test@test.com", display_name="Test")
    session.add(user)
    session.flush()

    stmt = StatementUpload(user_id=user.id, filename="test.pdf", bank_source="DBS / POSB", status="completed")
    session.add(stmt)
    session.flush()

    yield session, user, stmt
    session.close()


def _make_tx(session, statement, user, description, amount, ref=""):
    tx_hash = compute_transaction_hash("2026-01-01", description, amount, "DBS / POSB", ref)
    existing = (
        session.query(Transaction)
        .filter(Transaction.hash == tx_hash, Transaction.user_id == user.id)
        .first()
    )
    if existing:
        return None  # duplicate — skipped
    tx = Transaction(
        user_id=user.id,
        statement_id=statement.id,
        date=_TEST_DATE,
        description=description,
        amount=amount,
        type="debit",
        is_transfer=False,
        is_reviewed=False,
        hash=tx_hash,
    )
    session.add(tx)
    session.flush()
    return tx


def test_duplicate_transaction_is_skipped(db):
    session, user, stmt = db
    tx1 = _make_tx(session, stmt, user, "Grab", 12.50, "REF001")
    tx2 = _make_tx(session, stmt, user, "Grab", 12.50, "REF001")
    assert tx1 is not None
    assert tx2 is None  # duplicate skipped


def test_different_reference_ids_both_inserted(db):
    session, user, stmt = db
    tx1 = _make_tx(session, stmt, user, "Grab", 12.50, "REF001")
    tx2 = _make_tx(session, stmt, user, "Grab", 12.50, "REF002")
    assert tx1 is not None
    assert tx2 is not None  # different reference_id → different hash → both inserted


def test_same_transaction_different_users_both_inserted(db):
    """Two users uploading the same statement should each get their own copy."""
    session, user1, stmt1 = db

    # Create second user and statement
    user2 = User(google_id="test2", email="test2@test.com", display_name="Test2")
    session.add(user2)
    session.flush()
    stmt2 = StatementUpload(user_id=user2.id, filename="test.pdf", bank_source="DBS / POSB", status="completed")
    session.add(stmt2)
    session.flush()

    hash_val = compute_transaction_hash("2026-01-01", "Grab", 12.50, "DBS / POSB", "REF001")

    tx1 = _make_tx(session, stmt1, user1, "Grab", 12.50, "REF001")

    # Same hash but different user — should be allowed
    existing = (
        session.query(Transaction)
        .filter(Transaction.hash == hash_val, Transaction.user_id == user2.id)
        .first()
    )
    assert existing is None
    tx2 = Transaction(
        user_id=user2.id,
        statement_id=stmt2.id,
        date=_TEST_DATE,
        description="Grab",
        amount=12.50,
        type="debit",
        is_transfer=False,
        is_reviewed=False,
        hash=hash_val,
    )
    session.add(tx2)
    session.flush()

    assert tx1 is not None
    assert tx2.id is not None
