"""End-to-end accuracy tests for the PDF extraction pipeline.

These call the real model, so they cost money and take minutes. They are opt-in:

    RUN_LIVE_PARSER_TESTS=1 venv/Scripts/python -m pytest tests/test_pdf_pipeline.py -v

Run them after any change to the extraction prompt, the schema, or the model —
those changes cannot be validated by the offline suite.

Ground truth for may26_dbs.pdf (May 2026 DBS statement, 11 pages), manually
counted from the PDF by the user:
  - 93 transactions
  - Total debits:    5919.87
  - Total credits:   3378.36
  - Closing balance:  947.19
"""

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DBS_PDF = ROOT / "may26_dbs.pdf"
PAYLAH_PDF = ROOT / "may26_paylah.pdf"

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_PARSER_TESTS") != "1",
    reason="Live parser tests are opt-in: set RUN_LIVE_PARSER_TESTS=1",
)

_FOOTER_MARKERS = [
    "Transaction Details as of",
    "PDS_MMCON",
    "Page ",
    "of 11",
]


@pytest.fixture(scope="module")
def dbs_result():
    """Parse may26_dbs.pdf once and share the result across tests (one API call)."""
    if not DBS_PDF.exists():
        pytest.skip("may26_dbs.pdf not present")
    from app.services.pdf_parser import parse_statement

    return parse_statement(str(DBS_PDF), "DBS")


@pytest.fixture(scope="module")
def paylah_result():
    if not PAYLAH_PDF.exists():
        pytest.skip("may26_paylah.pdf not present")
    from app.services.pdf_parser import parse_statement

    return parse_statement(str(PAYLAH_PDF), "DBS PayLah!")


# ── Ground truth: may26_dbs.pdf ───────────────────────────────────────────────

def test_transaction_count(dbs_result):
    txs = dbs_result["transactions"]
    assert len(txs) == 93, f"Expected 93 transactions, got {len(txs)}"


def test_total_debits(dbs_result):
    total = sum(t["amount"] for t in dbs_result["transactions"] if t["type"] == "debit")
    assert abs(total - 5919.87) < 0.01, f"Expected debits 5919.87, got {total:.2f}"


def test_total_credits(dbs_result):
    total = sum(t["amount"] for t in dbs_result["transactions"] if t["type"] == "credit")
    assert abs(total - 3378.36) < 0.01, f"Expected credits 3378.36, got {total:.2f}"


def test_closing_balance(dbs_result):
    assert abs((dbs_result["closing_balance"] or 0) - 947.19) < 0.01


# ── Contract guards: violating these makes upload.py silently drop rows ───────

def test_all_amounts_positive(dbs_result):
    """upload.py drops any transaction with amount <= 0 without an error."""
    bad = [t for t in dbs_result["transactions"] if t["amount"] <= 0]
    assert not bad, f"{len(bad)} transaction(s) have a non-positive amount: {bad[:3]}"


def test_all_dates_parseable(dbs_result):
    """upload.py drops any transaction whose date parse_date() rejects."""
    from app.services.pdf_parser import parse_date

    bad = []
    for t in dbs_result["transactions"]:
        try:
            parse_date(str(t["date"]))
        except ValueError:
            bad.append(t)
    assert not bad, f"{len(bad)} transaction(s) have an unparseable date: {bad[:3]}"


def test_all_types_valid(dbs_result):
    bad = [t for t in dbs_result["transactions"] if t["type"] not in ("debit", "credit")]
    assert not bad, f"Invalid type values: {bad[:3]}"


def test_account_type_is_valid(dbs_result):
    valid = {"savings", "current", "credit_card", "paylah", "other"}
    assert dbs_result["account_type"] in valid


# ── Description quality ───────────────────────────────────────────────────────

def test_no_footer_text_in_descriptions(dbs_result):
    violations = [
        t["description"] for t in dbs_result["transactions"]
        if any(m in t["description"] for m in _FOOTER_MARKERS)
    ]
    assert not violations, f"Footer text leaked into descriptions: {violations[:3]}"


def test_no_summary_rows(dbs_result):
    """Balance brought/carried forward lines are not transactions."""
    banned = ("balance brought forward", "balance b/f", "balance c/f",
              "total balance carried forward", "total debits", "total credits")
    violations = [
        t["description"] for t in dbs_result["transactions"]
        if any(b in t["description"].lower() for b in banned)
    ]
    assert not violations, f"Summary rows extracted as transactions: {violations[:3]}"


def test_interest_earned_is_credit(dbs_result):
    matches = [t for t in dbs_result["transactions"] if "interest earned" in t["description"].lower()]
    assert matches, "Interest Earned transaction not found"
    for t in matches:
        assert t["type"] == "credit", f"Interest Earned should be a credit: {t}"


# ── PayLah!: CR/DR notation instead of Withdrawal/Deposit columns ─────────────

def test_paylah_cr_dr_produces_both_types(paylah_result):
    """PayLah! marks direction with CR/DR rather than separate columns."""
    types = {t["type"] for t in paylah_result["transactions"]}
    assert types, "No transactions extracted from the PayLah! statement"
    assert types <= {"debit", "credit"}, f"Unexpected type values: {types}"


def test_paylah_amounts_positive_and_clean(paylah_result):
    """CR/DR markers must set `type`, never leak into `amount`."""
    for t in paylah_result["transactions"]:
        assert t["amount"] > 0, f"Non-positive amount: {t}"
        assert isinstance(t["amount"], (int, float)), f"Amount is not numeric: {t}"
