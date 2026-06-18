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
| PDF Parsing | pdfplumber + OpenAI API | pdfplumber extracts raw text; GPT-4o mini structures it |
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

**Parsing approach:** pdfplumber → raw text → GPT-4o mini → structured JSON. No hardcoded regex. Raw text stored in DB so old statements can be re-parsed if prompt improves. **PDFs are deleted after successful parse** (raw_text is sufficient).

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
pdfplumber → extract raw text
    ↓
GPT-4o mini → structured JSON: [{ date, description, reference_id, amount, type, account_type, is_transfer }]
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

### GPT extraction — key prompt rules (`app/services/pdf_parser.py` SYSTEM_PROMPT)
- Extract EVERY row — never skip
- `description`: preserve type prefix (e.g. "Cash Withdrawal", "Salary"); strip card numbers (16-digit patterns); do NOT strip reference numbers — those go in `reference_id`
- `reference_id`: raw transaction reference number if visible; null otherwise
- `is_transfer`: true for wallet top-ups, inter-account transfers, credit card bill payments, own-account PayNow/FAST

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
SECRET_KEY=<64 hex chars — python -c "import secrets; print(secrets.token_hex(64))">
GOOGLE_CLIENT_ID=your_google_client_id
GOOGLE_CLIENT_SECRET=your_google_client_secret
ENVIRONMENT=development
```

Production (Render env vars — never in code):
```
OPENAI_API_KEY, DATABASE_URL, SECRET_KEY, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, ENVIRONMENT=production
```

---

## Production Checklist (Before Going Live)

- [ ] `ENVIRONMENT=production` set → enables sanitized error messages, `debug=False`
- [ ] **Enable GitHub Dependabot alerts** (Settings → Security → Dependabot) — reminder for when repo is pushed
- [ ] **`| safe` audit** — grep all templates and confirm no user-supplied content passes through `| safe`
- [ ] OpenAI key rotated for production (revoke dev key)
- [ ] All Render env vars set; no secrets in code or git

---

## Important Notes for Claude Code

- All datetime values: use `datetime.now(timezone.utc)` — `datetime.utcnow()` is deprecated in Python 3.12+
- SQLAlchemy column defaults: use `lambda: datetime.now(timezone.utc)` (not the evaluated value)
- Only `is_reviewed=True` transactions appear in dashboard analytics
- Amount values may be negative (legacy data); always use `abs()` / `|abs` filter when displaying — `type` field carries sign semantics
- **Zombie server warning:** check for zombie uvicorn processes with `netstat -ano | findstr :8000`; kill with `Stop-Process -Id <pid> -Force`
- `parse_statement_with_gpt` returns `dict`: `{"transactions": list, "closing_balance": float|None, "account_type": str|None}`. Old alias `parse_transactions_with_gpt` → list kept for backward compat.
- `batch_alter_table` in Alembic is SQLite-specific — new migrations targeting Postgres should use regular `op.add_column` etc. or check dialect
- `DATABASE_URL` from Render starts with `postgres://` — must replace with `postgresql://` for SQLAlchemy