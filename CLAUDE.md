# CLAUDE.md — Personal Finance Tracker

This file is the source of truth for Claude Code to continue development of this project.

---

## Project Overview

A personal finance tracker that started as a local single-user tool and is being built out into a deployable multi-user web app for friends/family (and potentially public).

Core features:
- Ingests monthly bank statements (PDF) from multiple Singapore banks
- Extracts, deduplicates, and categorizes transactions using AI (GPT-4o-mini)
- Tracks an investment portfolio (IBKR) with live price updates
- Displays an interactive dashboard with spending analytics, net worth, and portfolio performance

**Final goal:** Deployed on Render with Google OAuth, PostgreSQL, CI/CD via GitHub Actions. Mobile app (React Native) planned later — same FastAPI backend will serve a `/api/` JSON layer.

---

## UI / Design System

All UI must follow the dark dashboard design system. Key tokens:
- Surface: `#09090b` | Card: `#111113` | Text: `#fafafa` | Primary: `#0C5CAB`
- Success: `#10b981` | Warning: `#f59e0b` | Danger: `#ef4444`
- Font: IBM Plex Sans (primary), IBM Plex Mono (numbers/code)
- Cards use dark glass-panel style with subtle borders; inputs use dark backgrounds with border focus rings
- All pages must be mobile-responsive. Tables must scroll horizontally, never clip.

---

## Key Constraints & Decisions

### Deployment Target
- **Currently local** — will move to Render (web service + managed Postgres) once auth is implemented
- Google OAuth for authentication (session-based for web, JWT later for mobile)
- Multi-user: `user_id` FK on all user-data tables; Categories remain global/shared

### Tech Stack
| Layer | Technology | Reason |
|---|---|---|
| Backend | FastAPI (Python) | Lightweight, async, scales to auth + API layer |
| Database | SQLite locally → PostgreSQL on Render | Zero setup locally, managed Postgres in prod |
| Migrations | Alembic | Clean schema versioning |
| PDF Parsing | OpenAI Responses API (reasoning model, native PDF input) | One call per statement; no layout/coordinate logic |
| AI Categorization | OpenAI API (GPT-4o mini) | Cheap, accurate |
| Stock Prices | yfinance (free, no API key) | Real-time/delayed quotes for stocks, ETFs, FX |
| Frontend | Jinja2 + Vanilla JS + Chart.js | No build tooling needed |
| Auth | authlib + starlette sessions | Google OAuth, session cookie |

**Not using React** — Jinja2 SSR is sufficient for now. FastAPI backend unchanged when mobile/React is added.

**User's proficiency:** Python, SQL, HTML, CSS, some JS/React/Tailwind.

---

## Supported Banks / Statement Sources
- DBS / POSB
- DBS PayLah!
- Citibank
- Standard Chartered
- GXS
- MariBank
- And any future banks

**Parsing approach:** the PDF is sent natively to a reasoning model in a single Responses API call, which returns structured JSON validated against a strict schema. No hardcoded regex, no layout detection. **PDFs are deleted after a successful parse.**

> Note: `statement_uploads.raw_text` is a legacy column that is never written — the "Extract Balance" button in `upload.html` is gated on it and posts to a route that does not exist. Both are dead and pending cleanup.

---

## Database Schema

### `users` (added in migration 0014)
```
id, google_id (unique), email, display_name, created_at, last_login_at
```

### `statement_uploads`
```
id, user_id (FK → users), filename, bank_source, upload_date, raw_text, status,
closing_balance (Float, nullable), account_type (String, nullable),
skipped_json (Text, nullable — JSON list of transactions skipped as duplicates)
```

### `transactions`
```
id, user_id (FK → users), statement_id (FK, nullable),
date, description, amount, type (debit/credit),
category_id (FK), transaction_date, is_transfer, account_type, is_reviewed, hash,
reimbursement_category_id (FK, nullable),
split_start_month (String, nullable), split_end_month (String, nullable)
```
- `hash` = `sha256(date|description|amount|bank_source|reference_id)` — unique per `(hash, user_id)` composite
- `reference_id` from GPT output included in hash to distinguish same-merchant same-day same-amount transactions
- `is_transfer` = True for wallet top-ups, inter-account transfers (excluded from spending analytics)
- `is_reviewed` = False on insert; user sets True after manual review

### `categories` (global — not per-user)
```
id, name, display_name, is_transfer
```
Seeded categories: `food_dining`, `transport`, `shopping`, `entertainment`, `utilities_bills`, `healthcare`, `travel`, `education`, `personal_care`, `subscriptions`, `reimbursements`, `transfers` (is_transfer=True), `income`, `others`, `fun_money`, `groceries`, `cash_withdrawal`, `motorbike_maintenance`, `tech_electronics`

Notable: `groceries` → "Groceries" (not "Groceries / Supermarket")

### `categorization_examples` (per-user)
```
id, user_id (FK → users), description (unique per user), category_id (FK), created_at
```
- Unique constraint: `(description, user_id)` — each user's corrections are independent
- Injected into GPT prompt as few-shot examples on next upload

### `portfolio_positions`
```
id, user_id (FK → users), ticker, description, quantity, avg_cost_price, cost_basis, currency, last_synced_date
```

### `trade_log`
```
id, user_id (FK → users), ticker, trade_type (BUY/SELL), quantity, price, date, currency, notes
```

---

## Alembic Migration Chain

`19cf960c1918 → 0002 → 0003 → 0004 → 0005 → 0006 → 0007 → 0008 → 0009 → 0010 → 0011 → 0012 → 0013` (current HEAD, local SQLite)

- `0009` — adds `reimbursement_category_id` FK to `transactions`
- `0010` — adds `split_start_month`, `split_end_month` to `transactions`
- `0011` — adds `motorbike_maintenance` category
- `0012` — adds `skipped_json` to `statement_uploads`
- `0013` — adds `tech_electronics` category
- `0014` *(pending)* — adds `users` table + `user_id` FK to all tables + composite unique on `(hash, user_id)`

---

## PDF Parsing Pipeline

```
PDF Upload
    ↓
parse_statement() → ONE call: PDF sent natively to a reasoning model → structured JSON
                    Primary: gemini-3.6-flash. Falls back to gpt-5.6-terra (OpenAI
                    Responses API) if Gemini fails outright — see below.
    ↓
Deduplicator → compute hash (includes reference_id), check (hash, user_id) unique, skip if exists
    ↓
Insert transactions with is_reviewed=False, user_id=current_user.id
    ↓ (PDF deleted here)
Categorizer → transfers by rule; rest batch-sent to GPT with few-shot examples
    ↓
/review → user confirms/corrects; corrections saved to categorization_examples
    ↓
is_reviewed=True → visible in dashboard analytics
```

The model reads the PDF directly — there is no rendering, no text extraction, and no
coordinate/layout logic. This replaced a 3-stage pipeline (Vision layout detection →
PyMuPDF row extraction → GPT CSV structuring) whose geometry heuristics silently dropped
transactions. `pymupdf` is no longer a dependency.

### Two providers: Gemini primary, OpenAI fallback
`parse_statement()` tries `_parse_with_gemini()` first; if it raises for any reason
(including after Gemini's own retries are exhausted, or a truncated/incomplete response),
it falls back to `_parse_with_openai()` — the original single-provider implementation.
Both share the exact same `_EXTRACTION_PROMPT` and `_TRANSACTION_SCHEMA`, so accuracy is
identical regardless of which one serves a given upload.

Why: the Gemini API key is currently on the **free tier** (no Cloud Billing account
linked), which is deprioritized under load and returns `503 UNAVAILABLE` ("high demand")
more readily than the paid tier. The `google-genai` SDK does **not** retry transient
errors by default — passing `HttpRetryOptions()` (even with every field left at its
default) is required to opt into its built-in policy: 5 attempts, 1–60s exponential
backoff with jitter, retrying `408/429/500/502/503/504`. This is already wired up in
`_gemini_client()`. The OpenAI fallback exists as the second layer of defense on top of
that, for whatever gets past the retries.

To reduce how often the fallback fires, enable billing on the Gemini API key (a Cloud
Billing account/card in Google Cloud Console — separate from any consumer Gemini/Google
AI Pro subscription, which does **not** grant API access). This is an account-level
action outside the app; no code change is needed once it's done.

**`GEMINI_API_KEY` must be set wherever this runs — including Render.** If it's missing
in production, every Gemini attempt fails immediately and every upload silently runs
through the OpenAI fallback instead: uploads still work, but none of the cost savings
apply, and there's no visible error unless Render logs are checked for the
"Gemini parse failed ... falling back" warning.

### Contract — violating these silently drops rows in `app/routers/upload.py`
- `amount` must be **positive**; `if amount <= 0: continue` discards the row with no error.
  Direction lives in `type` (`"debit"`/`"credit"`), never in the sign.
- `date` must parse via `parse_date()` (emit ISO `YYYY-MM-DD`) or the row is dropped.
- `reference_id` feeds the dedup hash — it is the only thing separating two same-merchant,
  same-day, same-amount transactions.

### Key prompt rules (`_EXTRACTION_PROMPT` in `app/services/pdf_parser.py`)
- Extract EVERY row — never skip, summarise, or truncate
- **`type` comes from WHICH COLUMN the amount is in (or the CR/DR marker), never from the
  description wording.** A "Debit Card Transaction" row is often a refund in the Deposit
  column and must then be `credit`. Without this rule the model misclassifies refunds.
- CR/DR statements (PayLah!): the marker sets `type` and is stripped from `amount`
- `description`: preserve the type prefix and merchant name, but EXCLUDE anything that varies
  per transaction — the reference number, trailing date codes ("27APR"), card numbers, footers.
  It must be identical across visits to the same merchant, because
  `categorization_examples.description` is the key the categorizer learns on.
- `reference_id`: transaction reference/trace number if visible; null otherwise
- `is_transfer`: true for wallet top-ups, inter-account transfers, credit card bill payments

### Failure handling
`_assert_complete()` raises on an `incomplete` response (e.g. `max_output_tokens` hit) or empty
output, so a truncated extraction surfaces as a failed upload rather than a plausible-looking
partial result. Never soften this into a warning.

### Verifying prompt/model changes
Offline tests cannot catch extraction regressions. After any change to the prompt, schema or
model, run the live suite against the known-good statement:
```
RUN_LIVE_PARSER_TESTS=1 venv/Scripts/python -m pytest tests/test_pdf_pipeline.py -v
```
Ground truth for `may26_dbs.pdf`: **93 transactions, debits 5919.87, credits 3378.36,
closing balance 947.19** (manually counted from the PDF).

---

## Deduplication Logic

Hash: `sha256(date|description.lower()|amount:.2f|bank_source|reference_id)` — function `compute_transaction_hash()` in `app/services/pdf_parser.py`

- Uniqueness is per-user: `UNIQUE(hash, user_id)` composite constraint (not global)
- Intra-statement dedup: `seen_hashes` set in upload loop catches duplicates within same PDF before DB
- PayLah/DBS top-up dedup: both statement entries flagged `is_transfer=True`; excluded from spending analytics

---

## Manual Review Flow (`/review`)

- Bulk confirm, AJAX category edit, debit/credit type toggle, manual add, delete
- Category change also syncs `is_transfer` flag automatically
- `| safe` usages in templates: must be server-computed data only — never raw user/transaction content

---

## Dashboard

### Layout (top to bottom)
1. Page header — "Dashboard" + period label + two-month selector (primary vs comparison)
2. Account Balances card — closing balances with MoM comparison arrows
3. 4 stat cards — Total Spending, Savings Rate, Total Income, Transactions — each with MoM comparison
4. Bar (spending by category) + Donut (drill-down) — 50/50; comparison tick marks on bar chart
5. Insights card (rule-based)
6. Insights + Reimbursements row — GPT insight cards + reimbursement list with split modal
7. Monthly Trend line chart (12 months)

### Key behaviors
- **Income**: only `category.name = "income"` credits count — not all non-transfer credits
- **Reimbursements**: `get_summary_stats` + `get_monthly_trend` deduct ALL reimbursements; `get_spending_by_category` only deducts tagged ones (has `reimbursement_category_id`) — intentional asymmetry
- **Split reimbursements**: deducted pro-rata via `_months_in_range()` helper in `analytics.py`
- **`chartjs-plugin-datalabels`**: registered globally; every chart NOT using labels must set `plugins: { datalabels: { display: false } }`

---

## Development Phases

| Phase | Description | Status |
|---|---|---|
| 1 | Project setup, DB models, Alembic | ✅ Done |
| 2 | PDF upload & parsing pipeline | ✅ Done |
| 3 | Deduplication & transfer detection | ✅ Done |
| 4 | AI categorization + learning system | ✅ Done |
| 5 | Manual review UI | ✅ Done |
| 7 | Dashboard — spending analytics | ✅ Done |
| — | Transactions browse page | ✅ Done |
| — | Dashboard overhaul (comparison, drill-down, reimbursements, live refresh) | ✅ Done |
| — | Account balance tracking | ✅ Done |
| — | Dedup fix (reference_id in hash) | ✅ Done |
| — | Upload page: skipped duplicates + debit/credit totals | ✅ Done |
| **Next** | Security hardening + Git + Multi-user + Auth + Postgres + Render deploy | 🔄 In progress |
| 6 | Portfolio — trade entry + position tracking | ⬜ Deferred |
| 8 | Live portfolio dashboard widgets | ⬜ Deferred |
| 9 | GPT narrative insights | ⬜ Deferred |
| 10 | IBKR PDF parser | ⬜ Deferred |

**See `handoff.md` for the detailed step-by-step execution plan for the "Next" phase above.**

---

## Environment Variables

Local `.env`:
```
OPENAI_API_KEY=your_key_here
GEMINI_API_KEY=your_key_here          # from aistudio.google.com/apikey — primary PDF parser
SECRET_KEY=<64 hex chars — python -c "import secrets; print(secrets.token_hex(64))">
GOOGLE_CLIENT_ID=your_google_client_id
GOOGLE_CLIENT_SECRET=your_google_client_secret
ENVIRONMENT=development
```

Production (Render env vars — never in code):
```
OPENAI_API_KEY, GEMINI_API_KEY, DATABASE_URL, SECRET_KEY, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, ENVIRONMENT=production
```
`GEMINI_API_KEY` is unrelated to a consumer Gemini/Google AI Pro subscription — it's a
separate Google Cloud / AI Studio credential. See "Two providers" above: if it's absent,
uploads still work but silently run through the (more expensive) OpenAI fallback for every
statement.

---

## Production Checklist (Before Going Live)

- [ ] `ENVIRONMENT=production` set → enables sanitized error messages, `debug=False`
- [ ] **Enable GitHub Dependabot alerts** (Settings → Security → Dependabot) — reminder for when repo is pushed
- [ ] **`| safe` audit** — grep all templates and confirm no user-supplied content passes through `| safe`
- [ ] OpenAI and Gemini keys rotated for production (revoke dev keys)
- [ ] Gemini API billing enabled (Cloud Billing account linked) to move off the free tier's lower-priority queue, reducing OpenAI-fallback frequency
- [ ] All Render env vars set; no secrets in code or git

---

## Important Notes for Claude Code

- All datetime values: use `datetime.now(timezone.utc)` — `datetime.utcnow()` is deprecated in Python 3.12+
- SQLAlchemy column defaults: use `lambda: datetime.now(timezone.utc)` (not the evaluated value)
- Only `is_reviewed=True` transactions appear in dashboard analytics
- Amount values may be negative (legacy data); always use `abs()` / `|abs` filter when displaying — `type` field carries sign semantics
- **Zombie server warning:** check for zombie uvicorn processes with `netstat -ano | findstr :8000`; kill with `Stop-Process -Id <pid> -Force`
- `parse_statement(pdf_path, bank_source)` returns `dict`: `{"transactions": list, "closing_balance": float|None, "account_type": str|None}`. It is called from exactly one place — `app/routers/upload.py` Phase A — so the pipeline can be swapped without touching anything downstream.
- Parser config is env-overridable. Gemini (primary): `STATEMENT_PARSER_MODEL` (default `gemini-3.6-flash`), `STATEMENT_PARSER_GEMINI_THINKING` (`HIGH`). OpenAI (fallback): `STATEMENT_PARSER_FALLBACK_MODEL` (default `gpt-5.6-terra`), `STATEMENT_PARSER_FALLBACK_EFFORT` (`high`), `STATEMENT_PARSER_FALLBACK_SERVICE_TIER` (`default`). A statement takes ~90–215s to parse; `_PROCESSING_TIMEOUT` in `upload.py` is 15 min, sized to cover Gemini retries plus a full fallback attempt.
- `batch_alter_table` in Alembic is SQLite-specific — new migrations targeting Postgres should use regular `op.add_column` etc. or check dialect
- `DATABASE_URL` from Render starts with `postgres://` — must replace with `postgresql://` for SQLAlchemy