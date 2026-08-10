import base64
import hashlib
import io as _io
import csv as _csv
import json
import re as _re
from collections import defaultdict
from datetime import datetime

_MONEY_RE = _re.compile(r"^\d[\d,]*\.\d{2}$")

_NON_TXN_DESCS = (
    "balance brought forward", "balance b/f", "balance c/f",
    "opening balance", "closing balance", "balance carried forward",
    "total balance carried forward", "total balance",
    "total debits", "total credits",
)

_DATE_RE = _re.compile(
    r"\b\d{2}[/\-]\d{2}[/\-]\d{2,4}\b"  # 31/05/2026 or 31-05-26
    r"|\b\d{2}\s+\w{3}\s+\d{4}\b"        # 31 May 2026
)


def _is_money_value(s: str) -> bool:
    """Return True if s looks like a monetary amount (e.g. '1.40', '3,487.30')."""
    return bool(_MONEY_RE.match(s.strip())) if s else False


def _compute_row_spacing(below: list, col_defs: list[dict]) -> float:
    """Estimate row spacing from balance-column monetary value y-centers.

    Uses only balance-column words so multi-word description lines don't skew the median.
    Returns 0.0 if there are fewer than 3 balance values (can't estimate reliably).
    """
    balance_col = next((c for c in col_defs if c["field"] == "balance"), None)
    if not balance_col:
        return 0.0
    bal_ys = sorted([
        (w[1] + w[3]) / 2
        for w in below
        if balance_col["x_start"] <= (w[0] + w[2]) / 2 < balance_col["x_end"]
        and _MONEY_RE.match(w[4].strip())
    ])
    if len(bal_ys) < 3:
        return 0.0
    gaps = [bal_ys[i + 1] - bal_ys[i]
            for i in range(len(bal_ys) - 1)
            if bal_ys[i + 1] - bal_ys[i] > 1.5]
    return sorted(gaps)[len(gaps) // 2] if gaps else 0.0


def _has_date_value(s: str) -> bool:
    """Return True if s contains a recognisable date pattern."""
    return bool(_DATE_RE.search(s)) if s else False


def _clean_date_field(s: str) -> str:
    """Strip noise (e.g. 'No. ', '/ ') from a date column value, keeping only the date."""
    if not s:
        return s
    m = _DATE_RE.search(s)
    return m.group(0) if m else s

import pymupdf as fitz
from openai import OpenAI

# 90-second timeout per call. OpenAI default is 600s — a hung Vision request
# would otherwise block the background thread for up to 10 minutes silently.
client = OpenAI(timeout=90.0)

# ── Stage 1: Vision layout-detection schema ───────────────────────────────────
_LAYOUT_SYSTEM_PROMPT = """You are a bank statement layout analyser.
You will receive page images of a bank statement alongside native text lines extracted from each page (as JSON).
For each page, determine whether it contains a transaction table.
If it does, identify the semantic role of each column by matching the column header text exactly as it appears.

Do NOT extract any transaction values — only identify column structure.

Field values for 'field':
- "date": the transaction posting date column
- "description": the merchant / narrative description column
- "debit": money leaving the account (labelled Withdrawal, Debit, Dr, Payment, etc.)
- "credit": money entering the account (labelled Deposit, Credit, Cr, Receipt, etc.)
- "balance": running account balance (NOT a transaction amount)
- "other": any column that does not fit the above

If the page contains a transaction table, return table_bbox as [x0, y0, x1, y1] pixel coordinates
in the rendered image. The bbox must span from the TOP of the column header row down to the BOTTOM
of the LAST transaction data row — include the entire table body, not just the header. Exclude any
page footers, page numbers, or summary tables (e.g. "Total Balance Carried Forward") that appear
below the last transaction row. Return null for table_bbox if has_transaction_table is false."""

_LAYOUT_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "layout_detection",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "pages": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "page_number": {"type": "integer"},
                            "has_transaction_table": {"type": "boolean"},
                            "table_bbox": {
                                "anyOf": [
                                    {"type": "array", "items": {"type": "number"}},
                                    {"type": "null"},
                                ],
                            },
                            "columns": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "field": {
                                            "type": "string",
                                            "enum": ["date", "description", "debit", "credit", "balance", "other"],
                                        },
                                        "header_text": {"type": "string"},
                                    },
                                    "required": ["field", "header_text"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["page_number", "has_transaction_table", "table_bbox", "columns"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["pages"],
            "additionalProperties": False,
        },
    },
}

# ── Stage 3: CSV → structured JSON (gpt-4o-mini, text only) ──────────────────
_SYSTEM_PROMPT_CSV = """You are a financial data extractor for Singapore bank statements.
You will receive a CSV table extracted from a bank statement. The columns are already semantically labelled:
- "date": posting date
- "description": merchant or narrative
- "debit": amount leaving the account (always a debit)
- "credit": amount entering the account (always a credit)
- "balance": running account balance — never use this as a transaction amount

Rules:
- Extract EVERY transaction row — do not skip any
- amount: always a positive float (use the debit or credit value, not balance)
- date: YYYY-MM-DD
- transaction_date: YYYY-MM-DD or null
- type: "debit" if value came from the debit column, "credit" if from the credit column
- is_transfer: true for wallet top-ups, own-account transfers, credit card bill payments
- reference_id: if a standalone numeric reference (8+ digits, not a card or account number)
  appears in the description, extract it here; null if none
- account_type: infer from context — one of "savings", "current", "credit_card", "paylah", "other"
- closing_balance: the last balance value in the balance column, or null if not available
- description: merchant or narrative text only. Preserve the type prefix (e.g. "Debit Card
  Transaction", "FAST Payment / Receipt", "Salary"). Strip card numbers (patterns like
  "4628-4500-4754-4953"), bank registration codes (e.g. "SG400..."), page footer text
  ("Transaction Details as of...", "Page X of Y"), and any technical identifiers or trailing
  noise that is not part of the merchant name.
- Rows with no date are continuation lines — merge description with the preceding transaction
- Skip non-transaction rows: balance markers ("Balance Brought Forward", "Balance B/F",
  "Balance C/F"), totals ("Total Balance Carried Forward", "Total Debits/Credits"), and any
  summary row where the description signals a period summary rather than a merchant or payee"""

# ── Vision fallback: all pages as images → gpt-4o (original approach) ────────
SYSTEM_PROMPT = """You are a financial data extractor for Singapore bank statements.
Extract EVERY transaction from the provided bank statement and return a JSON object.
Do not skip any transaction row. Your output must contain exactly as many transactions as appear in the statement.
If unsure about a row, include it — never silently drop it.

Top-level fields:
- account_type: one of "savings", "current", "credit_card", "paylah", "other"
- closing_balance: final balance at END of statement period; null if not visible.

Each transaction must have:
- date: YYYY-MM-DD posting date
- transaction_date: YYYY-MM-DD or null
- description: preserve type prefix, remove card numbers, keep reference in reference_id
- amount: positive float
- type: "debit" or "credit"
- account_type: one of the enum values
- is_transfer: boolean
- reference_id: string or null

DETERMINING DEBIT vs CREDIT:
DBS/POSB: separate Withdrawal and Deposit columns.
- Withdrawal column value → type = "debit"
- Deposit column value → type = "credit"
- Balance column is running balance — never treat as transaction amount.
Credit cards: charges = debit, payments/refunds = credit.

is_transfer true: PayLah/GrabPay top-ups, own-account transfers, credit card bill payments, own-account PayNow/FAST.
is_transfer false: merchant spending, salary, interest, PayNow from friends."""

_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "bank_statement",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "account_type": {
                    "type": "string",
                    "enum": ["savings", "current", "credit_card", "paylah", "other"],
                },
                "closing_balance": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                "transactions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string"},
                            "transaction_date": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                            "description": {"type": "string"},
                            "amount": {"type": "number"},
                            "type": {"type": "string", "enum": ["debit", "credit"]},
                            "account_type": {
                                "type": "string",
                                "enum": ["savings", "current", "credit_card", "paylah", "other"],
                            },
                            "is_transfer": {"type": "boolean"},
                            "reference_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        },
                        "required": [
                            "date", "transaction_date", "description", "amount",
                            "type", "account_type", "is_transfer", "reference_id",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["account_type", "closing_balance", "transactions"],
            "additionalProperties": False,
        },
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_parsed_result(data: dict) -> dict:
    transactions = data.get("transactions", []) if isinstance(data, dict) else data
    if not isinstance(transactions, list):
        raise ValueError(f"Unexpected GPT response shape: {type(transactions)}")
    closing_balance = None
    if isinstance(data, dict):
        raw_cb = data.get("closing_balance")
        if raw_cb is not None:
            try:
                closing_balance = float(raw_cb)
            except (ValueError, TypeError):
                pass
    account_type = (data.get("account_type") or None) if isinstance(data, dict) else None
    return {"transactions": transactions, "closing_balance": closing_balance, "account_type": account_type}


def pdf_to_base64_images(pdf_path: str, dpi: int = 150) -> list[str]:
    """Render each PDF page to a base64-encoded PNG."""
    doc = fitz.open(pdf_path)
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    images = []
    for page in doc:
        pix = page.get_pixmap(matrix=mat)
        images.append(base64.b64encode(pix.tobytes("png")).decode())
    doc.close()
    if not images:
        raise ValueError("No pages could be rendered from this PDF.")
    return images


# ── Stage 1: Vision layout detection ─────────────────────────────────────────

def _get_page_text_lines(page, page_num: int) -> list[dict]:
    """Extract native text lines with bounding boxes from a PyMuPDF page.

    Groups words by their (block_no, line_no) to reconstruct multi-word lines.
    Returns compact dicts for inclusion in the Vision call payload.
    """
    words = page.get_text("words", sort=True)
    # Each word tuple: (x0, y0, x1, y1, word_text, block_no, line_no, word_no)

    line_buckets: dict[tuple, dict] = defaultdict(
        lambda: {"words": [], "x0": 1e9, "y0": 1e9, "x1": 0.0, "y1": 0.0}
    )
    for x0, y0, x1, y1, word_text, block_no, line_no, word_no in words:
        key = (int(block_no), int(line_no))
        line_buckets[key]["words"].append((int(word_no), word_text))
        line_buckets[key]["x0"] = min(line_buckets[key]["x0"], x0)
        line_buckets[key]["y0"] = min(line_buckets[key]["y0"], y0)
        line_buckets[key]["x1"] = max(line_buckets[key]["x1"], x1)
        line_buckets[key]["y1"] = max(line_buckets[key]["y1"], y1)

    result = []
    sorted_keys = sorted(line_buckets, key=lambda k: (line_buckets[k]["y0"], line_buckets[k]["x0"]))
    for i, key in enumerate(sorted_keys):
        data = line_buckets[key]
        text = " ".join(w for _, w in sorted(data["words"]))
        if text.strip():
            result.append({
                "id": f"p{page_num}_L{i}",
                "text": text.strip(),
                "x0": round(data["x0"], 1),
                "y0": round(data["y0"], 1),
                "x1": round(data["x1"], 1),
                "y1": round(data["y1"], 1),
            })
    return result


def _detect_layout(images: list[str], all_text_lines: list[list[dict]]) -> dict:
    """Stage 1: one Vision call to detect transaction table layout across all pages.

    Sends each page image alongside its native text lines so GPT can correlate
    visual column positions with exact text content without guessing values.
    """
    user_content: list[dict] = []
    for i, (img, lines) in enumerate(zip(images, all_text_lines), start=1):
        user_content.append({
            "type": "text",
            "text": f"=== Page {i} native text lines ===\n{json.dumps(lines, separators=(',', ':'))}",
        })
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{img}", "detail": "auto"},
        })
    user_content.append({
        "type": "text",
        "text": "For each page, identify whether it contains a transaction table and the semantic role of each column.",
    })

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": _LAYOUT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0,
        response_format=_LAYOUT_SCHEMA,
    )
    return json.loads(response.choices[0].message.content.strip())


# ── Stage 2: Deterministic PyMuPDF extraction ─────────────────────────────────

def _match_header_line(header_text: str, text_lines: list[dict]) -> dict | None:
    """Find the text line that best matches a column header string.

    Collects all candidates (exact, first-word, substring), then returns the one
    closest to the top of the page. This ensures column headers at the top of the
    table are preferred over any body or footer text that happens to contain the
    same word (e.g. "Balance Brought Forward" matching a "Balance" column lookup).
    """
    def normalise(s: str) -> str:
        return _re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()

    target = normalise(header_text)
    target_first = target.split()[0] if target else ""

    candidates: list[tuple[int, dict]] = []
    for line in text_lines:
        norm = normalise(line["text"])
        if norm == target:
            candidates.append((0, line))
        elif target_first and norm == target_first:
            candidates.append((1, line))
        elif target and (target in norm or norm in target):
            candidates.append((2, line))

    if not candidates:
        return None

    # Sort by y-center ascending (prefer topmost match), then by match quality.
    candidates.sort(key=lambda c: ((c[1]["y0"] + c[1]["y1"]) / 2, c[0]))
    return candidates[0][1]


def _find_column_boundaries(
    columns: list[dict], text_lines: list[dict], page_width: float
) -> list[dict]:
    """Derive x-boundaries for each semantic column from matched header positions.

    Returns list of dicts sorted left-to-right:
      {"field": str, "header_text": str, "x_start": float, "x_end": float,
       "header_y_bottom": float}
    Only includes columns whose header was found in the native text.
    """
    located = []
    for col in columns:
        if col["field"] == "other":
            continue
        match = _match_header_line(col["header_text"], text_lines)
        if match:
            x_center = (match["x0"] + match["x1"]) / 2
            located.append({
                "field": col["field"],
                "header_text": col["header_text"],
                "x_center": x_center,
                "header_y_bottom": match["y1"],
            })

    if not located:
        return []

    located.sort(key=lambda c: c["x_center"])

    # Midpoint boundaries
    boundaries = [0.0]
    for i in range(len(located) - 1):
        boundaries.append((located[i]["x_center"] + located[i + 1]["x_center"]) / 2)
    boundaries.append(page_width)

    result = []
    for i, col in enumerate(located):
        result.append({
            "field": col["field"],
            "header_text": col["header_text"],
            "x_start": boundaries[i],
            "x_end": boundaries[i + 1],
            "header_y_bottom": col["header_y_bottom"],
        })
    return result


def _extract_rows_from_page(
    page, col_defs: list[dict], table_y_end: float | None = None, y_tol: float = 8.0
) -> list[dict]:
    """Extract transaction rows from one page using column boundary definitions.

    Words below the header row (and above table_y_end if provided) are grouped
    into visual rows by y-proximity, then each word is assigned to the column
    containing its x-centre.
    """
    if not col_defs:
        return []

    table_y_start = max(c["header_y_bottom"] for c in col_defs)
    words = page.get_text("words", sort=True)
    below = [w for w in words if w[1] > table_y_start]

    if not below:
        return []

    # Adaptive y_tol from balance-column monetary values only.
    # One value per row → gaps equal row spacing (no multi-word description noise).
    row_spacing = _compute_row_spacing(below, col_defs)
    if row_spacing > 0:
        y_tol = min(max(row_spacing * 0.8, 4.0), 10.0)

    # Hard cutoff: when Vision's table_bbox accurately marks the end of a short table
    # (top 35% of the page), exclude words whose bottom edge is below that bound.
    # For full-page tables Vision undershoots table_y_end (~40% of actual height),
    # so the filter is skipped to avoid truncating real transactions.
    page_height = page.rect.height
    if table_y_end is not None and page_height > 0 and table_y_end / page_height < 0.35:
        below = [w for w in below if w[3] < table_y_end]
        if not below:
            return []

    money_fields = {"debit", "credit", "balance"}

    # Group into visual rows by y-center proximity with a money-column collision guard.
    # If a new word would land in a money column that's already occupied in the current
    # visual row, start a new row regardless of y-distance — two transactions can never
    # share the same money column.
    visual_rows: list[dict] = []
    for w in below:
        x0, y0, x1, y1, text = w[0], w[1], w[2], w[3], w[4]
        y_center = (y0 + y1) / 2
        x_center = (x0 + x1) / 2

        if visual_rows and abs(y_center - visual_rows[-1]["y"]) <= y_tol:
            new_field = next(
                (c["field"] for c in col_defs if c["x_start"] <= x_center < c["x_end"]),
                None,
            )
            if new_field in money_fields:
                occupied = {
                    next(
                        (c["field"] for c in col_defs if c["x_start"] <= xc < c["x_end"]),
                        None,
                    )
                    for xc, _ in visual_rows[-1]["words"]
                }
                if new_field in occupied:
                    visual_rows.append({"y": y_center, "words": [(x_center, text)]})
                    continue
            visual_rows[-1]["words"].append((x_center, text))
        else:
            visual_rows.append({"y": y_center, "words": [(x_center, text)]})

    date_col_exists = any(c["field"] == "date" for c in col_defs)
    rows: list[dict] = []

    for vrow in visual_rows:
        row: dict[str, list[str]] = {c["field"]: [] for c in col_defs}
        for x_center, text in vrow["words"]:
            for col in col_defs:
                if col["x_start"] <= x_center < col["x_end"]:
                    row[col["field"]].append(text)
                    break

        row_dict = {field: " ".join(texts).strip() for field, texts in row.items()}

        # Strip noise from date column (e.g. "No. 23/05/2026" → "23/05/2026")
        if date_col_exists and row_dict.get("date"):
            row_dict["date"] = _clean_date_field(row_dict["date"])

        has_debit   = _is_money_value(row_dict.get("debit", ""))
        has_credit  = _is_money_value(row_dict.get("credit", ""))
        has_balance = _is_money_value(row_dict.get("balance", ""))
        has_money   = has_debit or has_credit or has_balance
        has_date    = date_col_exists and _has_date_value(row_dict.get("date", ""))

        desc_lower = row_dict.get("description", "").strip().lower()

        # Description-first filter: drop any known non-transaction summary row regardless
        # of whether it has monetary values (catches "Total Balance Carried Forward" which
        # has debit+credit+balance totals and would otherwise become a fake anchor row).
        if desc_lower and any(kw in desc_lower for kw in _NON_TXN_DESCS):
            continue

        # Drop pure balance-marker rows with no debit/credit/date and no description.
        if has_balance and not has_debit and not has_credit and not has_date:
            if not desc_lower:
                continue

        if has_money or has_date:
            rows.append(row_dict)
        elif rows:
            # Continuation line: fold all non-empty cell text (in column order)
            # into the preceding anchor's description.
            continuation = " ".join(
                row_dict[c["field"]] for c in col_defs if row_dict.get(c["field"])
            )
            if continuation:
                rows[-1]["description"] = (
                    rows[-1].get("description", "") + " " + continuation
                ).strip()
        # else: pre-table header/footer lines — drop

    return rows


def _rows_to_csv(all_rows: list[dict], field_names: list[str]) -> str:
    output = _io.StringIO()
    writer = _csv.DictWriter(output, fieldnames=field_names, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(all_rows)
    return output.getvalue()


# ── Stage 3: GPT text parse ───────────────────────────────────────────────────

def _parse_csv_with_gpt(table_csv: str, bank_source: str) -> dict:
    """Send a labelled CSV to gpt-4o-mini for structured transaction extraction."""
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT_CSV},
            {"role": "user", "content": f"Bank: {bank_source}\n\n{table_csv}"},
        ],
        temperature=0,
        response_format=_RESPONSE_SCHEMA,
    )
    data = json.loads(response.choices[0].message.content.strip())
    return _extract_parsed_result(data)


# ── Vision fallback ───────────────────────────────────────────────────────────

def _parse_statement_vision_fallback(pdf_path: str, bank_source: str) -> dict:
    """Fallback: send all pages as images to gpt-4o to extract transactions directly."""
    images = pdf_to_base64_images(pdf_path)
    user_content = [
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}", "detail": "high"}}
        for img in images
    ]
    user_content.append({
        "type": "text",
        "text": f"Bank: {bank_source}\n\nExtract ALL transactions from ALL pages of this bank statement.",
    })
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0,
        response_format=_RESPONSE_SCHEMA,
    )
    data = json.loads(response.choices[0].message.content.strip())
    return _extract_parsed_result(data)


# ── Main entry point ──────────────────────────────────────────────────────────

def parse_statement(pdf_path: str, bank_source: str) -> dict:
    """Parse a bank statement PDF using a three-stage pipeline.

    Stage 1 (Vision): detect which pages have transaction tables and identify
                      column semantics (debit/credit/date/description/balance).
    Stage 2 (PyMuPDF): deterministically extract rows using native text
                       coordinates and the column boundaries from Stage 1.
    Stage 3 (gpt-4o-mini): clean and normalise the structured CSV into the
                            standard transaction JSON schema.

    Falls back to full Vision extraction if Stage 1 finds no tabular pages.
    """
    doc = fitz.open(pdf_path)
    pages = list(doc)

    # Render images and extract text lines for all pages
    mat = fitz.Matrix(150 / 72, 150 / 72)
    images = []
    all_text_lines = []
    for page_num, page in enumerate(pages, start=1):
        pix = page.get_pixmap(matrix=mat)
        images.append(base64.b64encode(pix.tobytes("png")).decode())
        all_text_lines.append(_get_page_text_lines(page, page_num))

    doc_for_extract = fitz.open(pdf_path)  # second open for extraction (pages already closed above)
    doc.close()

    # Stage 1: Vision layout detection
    layout = _detect_layout(images, all_text_lines)

    all_rows: list[dict] = []
    field_names: list[str] = []

    for page_info in layout.get("pages", []):
        if not page_info.get("has_transaction_table"):
            continue

        page_num = page_info["page_number"]  # 1-indexed
        columns = page_info.get("columns", [])
        if not columns:
            continue

        page = doc_for_extract[page_num - 1]
        text_lines = all_text_lines[page_num - 1]
        page_width = page.rect.width

        # Stage 2: derive boundaries and extract rows
        col_defs = _find_column_boundaries(columns, text_lines, page_width)
        if not col_defs:
            continue

        # Convert table_bbox bottom from image pixels (150 DPI) → PDF points
        _PDF_SCALE = 72.0 / 150.0
        bbox = page_info.get("table_bbox")
        table_y_end = bbox[3] * _PDF_SCALE if bbox else None

        rows = _extract_rows_from_page(page, col_defs, table_y_end=table_y_end)
        if rows:
            all_rows.extend(rows)
            if not field_names:
                field_names = [c["field"] for c in col_defs]

    doc_for_extract.close()

    if all_rows and field_names:
        table_csv = _rows_to_csv(all_rows, field_names)
        # Stage 3 reads type and amount directly from the labelled debit/credit
        # columns — no positional override needed (and positional zipping breaks
        # whenever Stage 3 skips or merges a row, causing misalignment).
        return _parse_csv_with_gpt(table_csv, bank_source)

    # Fallback: no tabular pages found → Vision extraction
    return _parse_statement_vision_fallback(pdf_path, bank_source)


# Backward-compat alias
parse_statement_with_vision = parse_statement


# ── Utility functions (unchanged) ─────────────────────────────────────────────

def parse_date(date_str: str):
    """Parse a date string into a Python date object."""
    date_str = date_str.strip()
    formats = [
        "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y",
        "%d %b %Y", "%d %B %Y", "%d/%m/%y", "%d %b %y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: '{date_str}'")


def compute_transaction_hash(
    date: str, description: str, amount: float, bank_source: str, reference_id: str = ""
) -> str:
    raw = f"{date}|{description.strip().lower()}|{amount:.2f}|{bank_source}|{reference_id.strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()
