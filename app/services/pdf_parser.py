import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

import pdfplumber
from openai import OpenAI

client = OpenAI()

SYSTEM_PROMPT = """You are a financial data extractor for Singapore bank statements.
Extract EVERY transaction from the provided bank statement text and return a JSON object.
Do not skip any transaction row. Your output must contain exactly as many transactions as appear in the statement.
If unsure about a row, include it — never silently drop it.

Return format:
{
  "account_type": <string>,
  "closing_balance": <positive float or null>,
  "transactions": [<transaction>, ...]
}

Top-level fields:
- account_type: the type of account this entire statement belongs to — one of "savings", "current", "credit_card", "paylah", "other"
- closing_balance: the final account balance at the END of this statement period. Look for labels like "Closing Balance", "Balance Carried Forward", "Balance C/F", "Ending Balance", or the last balance figure shown. Return null if no closing balance figure is visible in the statement.

Each transaction must have exactly these fields:
- date: string, YYYY-MM-DD — the posting/statement date (when the bank recorded it)
- transaction_date: string, YYYY-MM-DD, or null — the actual transaction date if visible in the description (e.g. "26 Feb" in "DEBIT CARD OPENAI ... 26 FEB"). Set null if no actual date is visible.
- description: string — the merchant or transaction description. Preserve any meaningful type prefix exactly as it appears (e.g. "Cash Withdrawal", "Salary", "Interest Earned", "FAST Payment") and append the merchant details. Remove card numbers (16-digit patterns like 4628-4500-4754-4953) and trailing whitespace. Do NOT remove the transaction reference number — that goes in reference_id instead.
- amount: positive float (always positive regardless of debit or credit)
- type: "debit" or "credit"
- account_type: one of "savings", "current", "credit_card", "paylah", "other"
- is_transfer: boolean
- reference_id: string or null — the raw transaction reference number exactly as it appears (e.g. "000002015171751", "REF123456"). Include ONLY the reference/transaction ID, not the card number. Set null if no reference number is visible.

Set is_transfer to true for:
- Top-ups from bank account to PayLah, GrabPay, or other e-wallets
- Incoming wallet top-ups / reversals back to bank account
- Transfers between the user's own accounts (e.g. savings to credit card payment)
- Credit card bill payments from a savings/current account
- PayNow or FAST transfers to/from the user's own accounts
- Interbank transfers that appear to be between own accounts

Set is_transfer to false for:
- All actual merchant spending (food, transport, shopping, etc.)
- Salary or employment income credits
- Interest earned
- PayNow/PayLah receipts from friends (these are reimbursements or income, not own-account transfers)

Return only the JSON object with no markdown, no explanation, no code fences."""


def extract_text_from_pdf(pdf_path: str) -> str:
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
    if not pages:
        raise ValueError(
            "No text could be extracted from this PDF. "
            "It may be a scanned image — please use a digital statement."
        )
    return "\n\n".join(pages)


def parse_statement_with_gpt(raw_text: str, bank_source: str) -> dict:
    """Parse a bank statement with GPT.

    Returns:
        {
            "transactions":     list[dict],
            "closing_balance":  float | None,
            "account_type":     str | None,
        }
    """
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Bank: {bank_source}\n\nStatement text:\n{raw_text}"},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content.strip()
    data = json.loads(content)

    # Transactions list
    if isinstance(data, dict):
        transactions = data.get("transactions", [])
    else:
        transactions = data
    if not isinstance(transactions, list):
        raise ValueError(f"Unexpected GPT response shape: {type(transactions)}")

    # Closing balance
    closing_balance: float | None = None
    if isinstance(data, dict):
        raw_cb = data.get("closing_balance")
        if raw_cb is not None:
            try:
                closing_balance = float(raw_cb)
            except (ValueError, TypeError):
                closing_balance = None

    # Statement-level account type
    account_type: str | None = None
    if isinstance(data, dict):
        account_type = data.get("account_type") or None

    return {
        "transactions": transactions,
        "closing_balance": closing_balance,
        "account_type": account_type,
    }


# Keep old name as alias for backwards compatibility (e.g. any ad-hoc scripts)
def parse_transactions_with_gpt(raw_text: str, bank_source: str) -> list[dict]:
    return parse_statement_with_gpt(raw_text, bank_source)["transactions"]


def parse_date(date_str: str):
    """Parse a date string from GPT into a Python date object."""
    date_str = date_str.strip()
    formats = [
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d %b %Y",
        "%d %B %Y",
        "%d/%m/%y",
        "%d %b %y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: '{date_str}'")


def compute_transaction_hash(date: str, description: str, amount: float, bank_source: str, reference_id: str = "") -> str:
    raw = f"{date}|{description.strip().lower()}|{amount:.2f}|{bank_source}|{reference_id.strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()
