"""Bank statement PDF -> structured transactions.

A single call to a reasoning model that reads the PDF natively. This replaced a
three-stage pipeline (Vision layout detection -> PyMuPDF coordinate extraction ->
GPT CSV structuring) whose geometry heuristics were a recurring source of silent
data loss.

Two providers are wired up:
  - Primary:  gemini-3.6-flash (Google), ~30% cheaper, verified accurate.
  - Fallback: gpt-5.6-terra (OpenAI), used only if Gemini fails outright, so a
              Gemini-side outage (its free tier returns transient 503s under
              load) never turns into a failed upload for the user.
"""

import base64
import hashlib
import json
import logging
import os
import traceback
from datetime import datetime
from pathlib import Path

from google import genai
from google.genai import types as gtypes
from openai import OpenAI

logger = logging.getLogger(__name__)

# ── Configuration: primary (Gemini) ───────────────────────────────────────────

_GEMINI_MODEL = os.environ.get("STATEMENT_PARSER_MODEL", "gemini-3.6-flash")
_GEMINI_THINKING_LEVEL = os.environ.get("STATEMENT_PARSER_GEMINI_THINKING", "HIGH")

# HttpOptions.timeout is in MILLISECONDS, unlike the OpenAI client's seconds --
# verified against google.genai.types.HttpOptions before using it (a wrong unit
# here would silently turn this into a ~300ms timeout that fails every call).
_GEMINI_TIMEOUT_MS = 300_000  # 5 min per attempt; observed calls run 90-215s

# No max_output_tokens cap is set for Gemini. Its "high" thinking used ~31.6k
# tokens (23.5k thinking + 8k output) on the ground-truth statement -- close
# enough to a 32k cap that it would risk truncation on a slightly longer one,
# the same trap that hit gpt-5.4-nano in the model comparison. Gemini 3.6 Flash
# supports up to 65,536 output tokens by default, which is used as-is instead.


def _gemini_client() -> genai.Client:
    """Build the Gemini client lazily (mirrors _openai_client: no credentials
    required at import time, only when a parse actually runs).

    Passing an HttpRetryOptions() (even with every field left at its own
    default) opts into the SDK's built-in retry policy: 5 attempts total, 1-60s
    exponential backoff with jitter, retrying exactly 408/429/500/502/503/504 --
    confirmed by reading google.genai._api_client.retry_args(). Omitting
    retry_options entirely (the SDK's own default) disables retries completely,
    which is what caused every "high demand" 503 to surface immediately instead
    of being retried.
    """
    return genai.Client(
        http_options=gtypes.HttpOptions(
            timeout=_GEMINI_TIMEOUT_MS,
            retry_options=gtypes.HttpRetryOptions(),
        )
    )


# ── Configuration: fallback (OpenAI) ──────────────────────────────────────────

_FALLBACK_MODEL = os.environ.get("STATEMENT_PARSER_FALLBACK_MODEL", "gpt-5.6-terra")
_FALLBACK_EFFORT = os.environ.get("STATEMENT_PARSER_FALLBACK_EFFORT", "high")
_FALLBACK_SERVICE_TIER = os.environ.get("STATEMENT_PARSER_FALLBACK_SERVICE_TIER", "default")

# A ~90-transaction statement produces ~12k output tokens, and high-effort
# reasoning tokens also count against this budget. Generous on purpose: hitting
# the cap truncates the transaction list, which _assert_complete turns into a
# hard failure rather than a silent partial result.
_FALLBACK_MAX_OUTPUT_TOKENS = 32000

# High-effort reasoning over a full statement runs for minutes, not seconds.
_FALLBACK_TIMEOUT = 600.0


def _openai_client() -> OpenAI:
    """Build the OpenAI client lazily.

    Constructing it at module scope would require OPENAI_API_KEY to be present
    at import time, which forces every test that imports the app to set a dummy key.
    """
    return OpenAI(timeout=_FALLBACK_TIMEOUT, max_retries=2)


# ── Output schema ─────────────────────────────────────────────────────────────

_ACCOUNT_TYPES = ["savings", "current", "credit_card", "paylah", "other"]

# Consumed by app/routers/upload.py. Every field is required and strict, so the
# model cannot omit a key that the insert loop reads.
_TRANSACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "account_type": {"type": "string", "enum": _ACCOUNT_TYPES},
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
                    "account_type": {"type": "string", "enum": _ACCOUNT_TYPES},
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
}

# ── Extraction prompt ─────────────────────────────────────────────────────────

# Rules 1-3 exist because app/routers/upload.py silently drops any transaction
# with amount <= 0 or an unparseable date. Getting these wrong loses data with
# no error, so they are stated first and repeated in the field list.
_EXTRACTION_PROMPT = """You are a precise data extraction assistant for Singapore bank statements.
The attached PDF is a single bank statement from: {bank}

Extract EVERY individual transaction. Analyse the tabular layout and use the visual alignment of
columns to decide which value belongs to which header (Date, Description, Withdrawal, Deposit,
Balance, etc.). One row per transaction. If a description wraps across multiple lines, concatenate
it into one continuous string.

CRITICAL RULES — violating these silently corrupts the data:

1. `amount` is ALWAYS a positive number. Never emit a negative value, never include currency
   symbols, thousands separators, or CR/DR text. Direction is carried by `type`, never by sign.

2. `type` is exactly "debit" or "credit", and is decided ONLY by WHICH COLUMN the amount sits
   in (or by the CR/DR marker) — NEVER by the wording of the description:
   - "debit"  = the amount appears in the Withdrawal / Debit / Paid Out column, or is marked DR
   - "credit" = the amount appears in the Deposit / Credit / Paid In column, or is marked CR
   Read the amount's horizontal position against the column headers and use that alone.
   IMPORTANT: a description's wording is NOT evidence of direction. A row labelled
   "Debit Card Transaction" is frequently a REFUND sitting in the Deposit column, and must then
   be "credit". Likewise "Funds Transfer" and "PayNow" rows occur in both directions. If the
   description seems to contradict the column, THE COLUMN WINS.
   Never place the same amount in both columns, and never read a value from the Balance column.

3. CR/DR notation: some statements (e.g. PayLah!) mark amounts with a "CR" or "DR" suffix or
   prefix instead of using separate Withdrawal/Deposit columns. Use that marker to set `type`,
   and strip the marker out of `amount` entirely — it must never appear in the numeric output.
   When the statement DOES have separate Withdrawal/Deposit columns, derive `type` from which
   column the value sits in, and use any CR/DR marker only to confirm it.

FIELDS:
- `date`: posting date, formatted YYYY-MM-DD.
- `transaction_date`: the actual transaction date as YYYY-MM-DD when the statement shows one
  distinct from the posting date; otherwise null.
- `description`: merchant or narrative text. PRESERVE the leading type prefix when present
  (e.g. "Debit Card Transaction", "FAST Payment / Receipt", "Funds Transfer", "Salary",
  "Interest Earned"), and preserve the merchant name including its country/city token
  (e.g. "CHAGEE SINGAPORE SGP").
  This field is used to recognise recurring merchants across statements, so it MUST be
  IDENTICAL for two visits to the same merchant. Therefore EXCLUDE everything that varies
  per transaction:
    * the reference / authorisation / trace number (that belongs in `reference_id` only —
      never repeat it here, e.g. "TF664201777704282661", "000002370939626")
    * trailing transaction-date codes such as "27APR", "28MAY"
    * card numbers (e.g. "4628-4500-4754-4953") and bank registration codes
    * page footers ("Transaction Details as of...", "Page X of Y", "PDS_MMCON...")
  Example: the row "Debit Card Transaction SHOPEE SINGAPORE MP SI SGP 27APR 000002370939626"
  yields description "Debit Card Transaction SHOPEE SINGAPORE MP SI SGP" and
  reference_id "000002370939626".
- `amount`: positive number (see rule 1).
- `type`: "debit" or "credit" (see rules 2-3).
- `reference_id`: the transaction reference or authorisation/trace number when visible in the
  row, otherwise null. This is the ONLY thing distinguishing two transactions with the same
  merchant, date and amount, so always extract it when present. Never use a card number.
- `is_transfer`: true for movements between the user's own accounts — wallet/PayLah! top-ups,
  inter-account transfers, credit card bill payments. False for merchant spending, salary,
  interest earned, and incoming PayNow from other people.
- `account_type`: one of savings, current, credit_card, paylah, other.

STATEMENT-LEVEL FIELDS:
- `closing_balance`: the final closing balance of the statement, or null if not shown.
- `account_type`: the account type for the statement as a whole.

EXCLUDE non-transaction rows entirely: "Balance Brought Forward", "Balance B/F", "Balance C/F",
"Total Balance Carried Forward", opening/closing balance lines, subtotals, period summaries,
page headers and footers, bank addresses and marketing text.

Extract every transaction row. Do not skip, summarise, or truncate the list."""


# ── Parsing ───────────────────────────────────────────────────────────────────

def _assert_openai_complete(response) -> None:
    """Raise if the OpenAI response was truncated or empty.

    A partial extraction that reports success is the worst possible outcome: it
    reaches the review page looking plausible. Raising here triggers the
    fallback-of-the-fallback: upload.py's Phase A handler marks the statement
    "failed".
    """
    status = getattr(response, "status", None)
    if status == "incomplete":
        reason = getattr(getattr(response, "incomplete_details", None), "reason", "unknown")
        raise ValueError(
            f"Model returned an incomplete response (reason: {reason}). "
            f"The transaction list was cut off, so the result is not trustworthy."
        )
    if not (response.output_text or "").strip():
        raise ValueError(f"Model returned no output (status: {status}).")


def _assert_gemini_complete(response) -> None:
    """Raise if the Gemini response was truncated, blocked, or empty.

    Any failure here is caught by parse_statement() and triggers the OpenAI
    fallback -- a truncated Gemini result is never accepted as-is.
    """
    finish_reason = None
    try:
        finish_reason = response.candidates[0].finish_reason
    except (AttributeError, IndexError, TypeError):
        pass

    text = getattr(response, "text", None)
    if not text or not text.strip():
        raise ValueError(f"Gemini returned no output (finish_reason={finish_reason}).")

    ok_reasons = (gtypes.FinishReason.STOP, gtypes.FinishReason.FINISH_REASON_UNSPECIFIED, None)
    if finish_reason not in ok_reasons:
        raise ValueError(
            f"Gemini response did not finish cleanly (finish_reason={finish_reason}). "
            f"The transaction list may be cut off, so the result is not trustworthy."
        )


def _extract_parsed_result(data: dict) -> dict:
    """Normalise the model's JSON into the dict shape upload.py consumes."""
    transactions = data.get("transactions", []) if isinstance(data, dict) else data
    if not isinstance(transactions, list):
        raise ValueError(f"Unexpected response shape: {type(transactions)}")
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


def _parse_with_gemini(pdf_path: str, bank_source: str) -> dict:
    pdf_file = Path(pdf_path)
    pdf_bytes = pdf_file.read_bytes()
    thinking_level = getattr(
        gtypes.ThinkingLevel, _GEMINI_THINKING_LEVEL.upper(), gtypes.ThinkingLevel.HIGH
    )

    logger.info(
        "Parsing %s (%s, %.1f MB) with %s (thinking=%s)",
        pdf_file.name, bank_source, pdf_file.stat().st_size / 1_048_576,
        _GEMINI_MODEL, _GEMINI_THINKING_LEVEL,
    )

    response = _gemini_client().models.generate_content(
        model=_GEMINI_MODEL,
        contents=[
            gtypes.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
            gtypes.Part.from_text(text=_EXTRACTION_PROMPT.format(bank=bank_source)),
        ],
        config=gtypes.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=_TRANSACTION_SCHEMA,
            thinking_config=gtypes.ThinkingConfig(thinking_level=thinking_level),
        ),
    )

    _assert_gemini_complete(response)

    usage = getattr(response, "usage_metadata", None)
    if usage:
        logger.info(
            "Parsed %s with %s: %s input + %s output tokens (%s thinking)",
            pdf_file.name, _GEMINI_MODEL, usage.prompt_token_count,
            usage.candidates_token_count, usage.thoughts_token_count or 0,
        )

    try:
        data = json.loads(response.text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Gemini returned invalid JSON: {exc}") from exc

    result = _extract_parsed_result(data)
    logger.info(
        "Extracted %d transactions from %s (gemini)", len(result["transactions"]), pdf_file.name
    )
    return result


def _parse_with_openai(pdf_path: str, bank_source: str) -> dict:
    """Fallback path, used only when Gemini fails outright (see parse_statement)."""
    pdf_file = Path(pdf_path)
    with open(pdf_file, "rb") as f:
        b64_pdf = base64.b64encode(f.read()).decode()

    logger.info(
        "Parsing %s (%s, %.1f MB) with %s (fallback, effort=%s)",
        pdf_file.name, bank_source, pdf_file.stat().st_size / 1_048_576,
        _FALLBACK_MODEL, _FALLBACK_EFFORT,
    )

    response = _openai_client().responses.create(
        model=_FALLBACK_MODEL,
        reasoning={"effort": _FALLBACK_EFFORT},
        service_tier=_FALLBACK_SERVICE_TIER,
        max_output_tokens=_FALLBACK_MAX_OUTPUT_TOKENS,
        text={
            "format": {
                # Responses API uses a flat shape here — `name` sits at the top
                # level, not nested under a "json_schema" key as in Chat Completions.
                "type": "json_schema",
                "name": "bank_statement",
                "strict": True,
                "schema": _TRANSACTION_SCHEMA,
            }
        },
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": _EXTRACTION_PROMPT.format(bank=bank_source)},
                    {
                        "type": "input_file",
                        "filename": pdf_file.name,
                        "file_data": f"data:application/pdf;base64,{b64_pdf}",
                    },
                ],
            }
        ],
    )

    usage = getattr(response, "usage", None)
    if usage:
        reasoning = getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0)
        logger.info(
            "Parsed %s with %s (fallback): %s input + %s output tokens (%s reasoning)",
            pdf_file.name, _FALLBACK_MODEL, usage.input_tokens, usage.output_tokens, reasoning,
        )

    _assert_openai_complete(response)

    try:
        data = json.loads(response.output_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model returned invalid JSON: {exc}") from exc

    result = _extract_parsed_result(data)
    logger.info(
        "Extracted %d transactions from %s (fallback)", len(result["transactions"]), pdf_file.name
    )
    return result


def parse_statement(pdf_path: str, bank_source: str) -> dict:
    """Extract all transactions from a bank statement PDF.

    Returns {"transactions": list[dict], "closing_balance": float|None,
             "account_type": str|None}, where each transaction carries
    date, transaction_date, description, amount, type, account_type,
    is_transfer and reference_id.

    Tries Gemini first (cheaper, verified accurate). Falls back to OpenAI if
    Gemini fails outright — including after its own retries on transient
    errors are exhausted, or if its response is truncated/incomplete — so a
    Gemini-side outage never surfaces as a failed upload. Only raises if BOTH
    providers fail; never returns partial data from either.
    """
    try:
        return _parse_with_gemini(pdf_path, bank_source)
    except Exception:
        logger.warning(
            "Gemini parse failed for %s; falling back to %s.\n%s",
            Path(pdf_path).name, _FALLBACK_MODEL, traceback.format_exc(),
        )
        return _parse_with_openai(pdf_path, bank_source)


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
