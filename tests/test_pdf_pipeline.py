"""Regression tests for the Stage 2 PDF extraction pipeline.

Uses a frozen Stage 1 layout fixture (tests/fixtures/may26_dbs_layout.json) so
no OpenAI API calls are made during testing.  Tests run only when may26_dbs.pdf
is present in the project root (it is gitignored).

To regenerate the fixture after intentional pipeline changes:
    venv\\Scripts\\python tests/generate_layout_fixture.py

Ground truth for may26_dbs.pdf (May 2026 DBS statement, 11 pages):
  - 93 transaction rows extracted by Stage 2
  - Total withdrawals: 5919.87
  - Total deposits:    3378.36
"""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PDF_PATH = ROOT / "may26_dbs.pdf"
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "may26_dbs_layout.json"

# Skip the whole module if the PDF is absent (CI environment)
pytestmark = pytest.mark.skipif(
    not PDF_PATH.exists(),
    reason="may26_dbs.pdf not present — PDF pipeline tests run locally only",
)

_FOOTER_MARKERS = [
    "Transaction Details as of",
    "PDS_MMCON",
    "Page ",
    "of 11",
]


@pytest.fixture(scope="module")
def stage2_rows():
    """Run Stage 2 (deterministic PyMuPDF extraction) using the frozen Stage 1 fixture.

    Returns (all_rows, field_names) where all_rows is the list of raw row dicts
    before Stage 3 GPT normalisation — each row has 'date', 'description',
    'debit', 'credit', 'balance', '_type', '_amount' keys.
    """
    if not FIXTURE_PATH.exists():
        pytest.skip(
            f"Layout fixture not found at {FIXTURE_PATH}. "
            "Run: venv\\Scripts\\python tests/generate_layout_fixture.py"
        )

    import fitz
    from app.services.pdf_parser import (
        _extract_rows_from_page,
        _find_column_boundaries,
        _get_page_text_lines,
        _is_money_value,
    )

    layout = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    doc = fitz.open(str(PDF_PATH))
    all_text_lines = [_get_page_text_lines(doc[i], i + 1) for i in range(len(doc))]

    all_rows: list[dict] = []
    field_names: list[str] = []

    _PDF_SCALE = 72.0 / 150.0

    for page_info in layout.get("pages", []):
        if not page_info.get("has_transaction_table"):
            continue
        page_num = page_info["page_number"]
        columns = page_info.get("columns", [])
        if not columns:
            continue

        page = doc[page_num - 1]
        text_lines = all_text_lines[page_num - 1]
        page_width = page.rect.width

        bbox = page_info.get("table_bbox")
        col_defs = _find_column_boundaries(
            columns, text_lines, page_width,
            table_y_start=bbox[1] if bbox else None,
            table_x_start=bbox[0] if bbox else None,
        )
        if not col_defs:
            continue

        table_y_end = bbox[3] * _PDF_SCALE if bbox else None

        rows = _extract_rows_from_page(page, col_defs, table_y_end=table_y_end)
        if rows:
            # Mirror what parse_statement does: embed _type and _amount
            for row in rows:
                dv = row.get("debit", "")
                cv = row.get("credit", "")
                if _is_money_value(dv):
                    row["_type"] = "debit"
                    row["_amount"] = dv
                elif _is_money_value(cv):
                    row["_type"] = "credit"
                    row["_amount"] = cv
                else:
                    row["_type"] = ""
                    row["_amount"] = ""
            all_rows.extend(rows)
            if not field_names:
                field_names = [c["field"] for c in col_defs]

    doc.close()
    return all_rows, field_names


# ── Row count ─────────────────────────────────────────────────────────────────

def test_row_count(stage2_rows):
    rows, _ = stage2_rows
    assert len(rows) == 93, (
        f"Expected 93 rows, got {len(rows)}.\n"
        f"Row descriptions: {[r.get('description','') for r in rows]}"
    )


# ── Financial totals ──────────────────────────────────────────────────────────

def test_total_withdrawals(stage2_rows):
    from app.services.pdf_parser import _is_money_value
    rows, _ = stage2_rows
    total = sum(
        float(r["debit"].replace(",", ""))
        for r in rows
        if _is_money_value(r.get("debit", ""))
    )
    assert abs(total - 5919.87) < 0.01, (
        f"Expected total withdrawals 5919.87, got {total:.2f}"
    )


def test_total_deposits(stage2_rows):
    from app.services.pdf_parser import _is_money_value
    rows, _ = stage2_rows
    total = sum(
        float(r["credit"].replace(",", ""))
        for r in rows
        if _is_money_value(r.get("credit", ""))
    )
    assert abs(total - 3378.36) < 0.01, (
        f"Expected total deposits 3378.36, got {total:.2f}"
    )


# ── Description quality ───────────────────────────────────────────────────────

def test_no_footer_text_in_descriptions(stage2_rows):
    rows, _ = stage2_rows
    violations = []
    for r in rows:
        desc = r.get("description", "")
        for marker in _FOOTER_MARKERS:
            if marker in desc:
                violations.append(f"Marker {marker!r} found in: {desc!r}")
    assert not violations, (
        f"{len(violations)} footer violation(s):\n" + "\n".join(violations)
    )


def test_interest_earned_no_noise(stage2_rows):
    """Interest Earned row must be a credit with no '4' digit noise appended."""
    rows, _ = stage2_rows
    matches = [r for r in rows if "interest earned" in r.get("description", "").lower()]
    assert matches, "Interest Earned row not found"
    for r in matches:
        desc = r.get("description", "")
        assert r.get("_type") == "credit", f"Interest Earned should be credit, got: {r}"
        # The DBS separator glyphs render as "4" characters — ensure none are in the desc
        noise = [tok for tok in desc.split() if tok == "4"]
        assert not noise, f"Digit noise '4' found in Interest Earned description: {desc!r}"


# ── Specific transaction integrity ────────────────────────────────────────────

def test_paylah_topup_03_may(stage2_rows):
    """Funds Transfer TOP-UP TO PAYLAH on 03/05/2026 must be a 6.50 withdrawal."""
    rows, _ = stage2_rows
    matches = [
        r for r in rows
        if "03/05" in r.get("date", "")
        and "PAYLAH" in r.get("description", "").upper()
        and r.get("_type") == "debit"
    ]
    assert matches, (
        "PayLah top-up (03/05, debit, 'PAYLAH' in description) not found.\n"
        f"Rows on 03/05: {[r for r in rows if '03/05' in r.get('date', '')]}"
    )
    for r in matches:
        amount = float(r["_amount"].replace(",", ""))
        assert abs(amount - 6.50) < 0.01, (
            f"Expected PayLah top-up amount 6.50, got {amount} in row: {r}"
        )


def test_funds_transfer_chin_se_seong_no_footer(stage2_rows):
    """Funds transfer to CHIN SE SEONG must not contain footer text."""
    rows, _ = stage2_rows
    matches = [
        r for r in rows
        if "CHIN SE SEONG" in r.get("description", "").upper()
        or "FT260503MB46959841" in r.get("description", "")
    ]
    assert matches, "Funds transfer to CHIN SE SEONG / FT260503MB46959841 not found"
    for r in matches:
        desc = r.get("description", "")
        for marker in _FOOTER_MARKERS:
            assert marker not in desc, (
                f"Footer marker {marker!r} found in funds transfer description: {desc!r}"
            )


# ── Data integrity ────────────────────────────────────────────────────────────

def test_no_dual_money_rows(stage2_rows):
    """No row should have both a debit AND a credit value — that indicates a summary row
    (e.g. 'Total Balance Carried Forward') that should have been filtered out."""
    from app.services.pdf_parser import _is_money_value
    rows, _ = stage2_rows
    violations = [
        r for r in rows
        if _is_money_value(r.get("debit", "")) and _is_money_value(r.get("credit", ""))
    ]
    assert not violations, (
        f"{len(violations)} row(s) have both debit and credit values "
        f"(summary rows not filtered):\n"
        + "\n".join(str(r) for r in violations)
    )


def test_type_amount_consistency(stage2_rows):
    """Every row's _type must match which column (_debit or _credit) has the value."""
    from app.services.pdf_parser import _is_money_value
    rows, _ = stage2_rows
    errors = []
    for i, r in enumerate(rows):
        t = r.get("_type", "")
        has_debit = _is_money_value(r.get("debit", ""))
        has_credit = _is_money_value(r.get("credit", ""))
        if t == "debit" and not has_debit:
            errors.append(f"Row {i}: _type=debit but no debit value: {r}")
        if t == "credit" and not has_credit:
            errors.append(f"Row {i}: _type=credit but no credit value: {r}")
        if t == "" and (has_debit or has_credit):
            errors.append(f"Row {i}: _type='' but has money value: {r}")
    assert not errors, "\n".join(errors)
