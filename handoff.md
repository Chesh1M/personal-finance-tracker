# Handoff — Next Steps: Multi-User, Auth, Deployment

## Final Goal

Personal finance tracker → deployed web app supporting friends/family (and eventually public).
Stack: FastAPI + Jinja2 + PostgreSQL on Render, Google OAuth, CI/CD via GitHub Actions.
Mobile app (React Native) planned later — same FastAPI backend will serve a JSON API.

---

## Completed (as of 2026-06-02)

- Phases 1–5 + 7: upload pipeline, dedup, categorization, review UI, dashboard
- Dashboard overhaul: two-month comparison, bar+donut drill-down, reimbursements, account balances, comparison bars (custom tick plugin)
- Dedup fix: `reference_id` field in GPT output included in hash to prevent false positives within same-merchant same-day same-amount transactions
- Upload page: skipped duplicates visible with expandable detail, debit/credit totals per statement
- Categories added: Tech / Electronics (`tech_electronics`), Motorbike Maintenance (`motorbike_maintenance`)
- Alembic migration chain HEAD: `0013_add_tech_electronics_category`

---

## Account Setup (Do These First — One Time)

### 1. GitHub (private repo)
- Create a **private** repository at github.com
- Push current local code:
  ```
  git init
  git add .
  git commit -m "Initial commit: personal finance tracker phases 1-7 complete"
  git remote add origin https://github.com/yourusername/finance-tracker.git
  git push -u origin main
  ```
- Confirm `.gitignore` covers: `finance_tracker.db`, `uploads/`, `.env`, `venv/`, `__pycache__/`, `*.pyc`, `*.db`

### 2. Google Cloud Console (OAuth)
- Go to console.cloud.google.com → Create new project (e.g. "Finance Tracker")
- APIs & Services → OAuth consent screen → External → fill app name, support email, developer email
- APIs & Services → Credentials → Create OAuth 2.0 Client ID → Web application
- Authorized redirect URIs:
  - `http://localhost:8000/auth/callback` (dev)
  - `https://yourapp.onrender.com/auth/callback` (prod — add after Render setup)
- Copy client ID and client secret → store in `.env`:
  ```
  GOOGLE_CLIENT_ID=your_client_id
  GOOGLE_CLIENT_SECRET=your_client_secret
  ```
- Generate a `SECRET_KEY` for session signing:
  ```
  python -c "import secrets; print(secrets.token_hex(64))"
  ```
  Store in `.env`: `SECRET_KEY=<generated value>`

### 3. Render (hosting + Postgres)
- Create account at render.com
- Create a new **PostgreSQL** instance (free tier, 1GB)
  - Note the **External Database URL** — this is your `DATABASE_URL`
- Create a new **Web Service**
  - Connect to your GitHub repo
  - Runtime: Python 3.12
  - Build command: `pip install -r requirements.txt && alembic upgrade head`
  - Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
  - Add environment variables:
    - `OPENAI_API_KEY`
    - `DATABASE_URL` (from Postgres instance)
    - `GOOGLE_CLIENT_ID`
    - `GOOGLE_CLIENT_SECRET`
    - `SECRET_KEY`
    - `ENVIRONMENT=production`
- Note the Render-assigned URL → add to Google OAuth redirect URIs

---

## Implementation Phases

Work through these in order. Claude Code implements all code changes.

---

### Phase A — Security Hardening (do locally before any git push)

**A1. File upload validation** (`app/routers/upload.py`)
- Read first 4 bytes of uploaded file to verify PDF magic bytes (`%PDF`) — reject anything that isn't a real PDF
- Add 10MB size limit: reject if `file.size > 10 * 1024 * 1024`

**A2. Sanitize error responses** (`app/routers/upload.py`)
- Currently exception handler returns `str(e)` which may expose internal paths/details
- Add `ENVIRONMENT` env var check: in `production`, return a generic error message instead of the raw exception string

**A3. `| safe` audit** (Claude to do — search all templates before going public)
- Grep all templates for `| safe` and confirm every occurrence is server-computed data, never raw user input or transaction descriptions

---

### Phase B — Git Setup

- `git init` in project root (if not already)
- Confirm `.gitignore` is correct (DB, uploads, .env, venv, __pycache__)
- Initial commit and push to private GitHub repo (see Account Setup above)

---

### Phase C — Multi-User Schema (biggest change)

**Architecture decisions locked in:**
- Categories stay **global** (shared system categories, not per-user) — simplifies all analytics
- `user_id` added to: `statement_uploads`, `transactions`, `categorization_examples`, `portfolio_positions`, `trade_log`
- `transactions.hash` unique constraint: from global `UNIQUE(hash)` → per-user `UNIQUE(hash, user_id)`
  - Allows two different users to independently upload the same statement
- Nullable `user_id` during migration (backfilled immediately to seed user)

**C1. New `users` table** (`app/models.py`)
```python
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    google_id = Column(String, unique=True, nullable=False, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    display_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_login_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
```

**C2. Add `user_id` FK to all tables** (`app/models.py`)
- `StatementUpload.user_id = Column(Integer, ForeignKey("users.id"), nullable=True)`
- `Transaction.user_id = Column(Integer, ForeignKey("users.id"), nullable=True)`
- `CategorizationExample.user_id = Column(Integer, ForeignKey("users.id"), nullable=True)`
- `PortfolioPosition.user_id = Column(Integer, ForeignKey("users.id"), nullable=True)`
- `TradeLog.user_id = Column(Integer, ForeignKey("users.id"), nullable=True)`
- Change `Transaction.hash`: remove `unique=True`; add `__table_args__ = (UniqueConstraint("hash", "user_id", name="uq_transaction_hash_user"),)`

**C3. Alembic migration** (`0014_add_users_and_user_id`)
- Create `users` table
- Add nullable `user_id` to all tables
- Drop old global unique constraint on `transactions.hash`
- Add composite unique constraint `(hash, user_id)`
- Data migration within the script: insert seed user (`google_id="local_dev"`, `email="local@dev"`) and `UPDATE` all existing rows to `user_id = 1`
- SQLite uses `batch_alter_table` for constraint changes (existing pattern — follow it)

**C4. Update `app/services/analytics.py`** — every function adds `user_id: int` param
All 9 functions add `.filter(Transaction.user_id == user_id)` (or `StatementUpload.user_id == user_id`) to their queries:
`get_available_months`, `get_summary_stats`, `get_spending_by_category`, `get_category_transaction_details`, `get_account_balances`, `get_monthly_trend`, `get_reimbursements`, `get_insights`

**C5. Update all routers** — pass `current_user.id` (from Phase D auth) as `user_id`
- `app/routers/dashboard.py` — pass to all analytics calls
- `app/routers/upload.py` — set `user_id` on new Statement + Transaction objects; scope dedup query to user
- `app/routers/transactions.py` — add user_id filter to all queries; validate ownership before any PUT/DELETE
- `app/routers/balances.py` — filter statements by user_id

**C6. Cross-user deduplication** (`app/routers/upload.py`)
- DB dedup query: add `.filter(Transaction.user_id == user_id)` so users don't see each other's data as collisions
- Manual transaction dedup (in `add_transaction`): also scoped per user

---

### Phase D — Auth (Google OAuth)

**Install:**
```
authlib
httpx
itsdangerous
starlette  # already present
```

**D1. Session middleware** (`app/main.py`)
```python
from starlette.middleware.sessions import SessionMiddleware
app.add_middleware(SessionMiddleware, secret_key=os.environ["SECRET_KEY"])
```

**D2. Auth routes** (new file `app/routers/auth.py`)
- `GET /login` → renders `login.html` (just a "Sign in with Google" button, no access to app without auth)
- `GET /auth/google` → build Google OAuth URL via authlib, redirect to it
- `GET /auth/callback` → receive code from Google; exchange for token; fetch user profile (email, name, google_id); `get_or_create` User in DB; store `user_id` in session; redirect to `/dashboard`
- `GET /logout` → `request.session.clear()`; redirect to `/login`

**D3. `get_current_user` dependency** (`app/dependencies.py`)
```python
async def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user
```

**D4. Apply to all routes** — every existing route adds `current_user: User = Depends(get_current_user)`, then passes `current_user.id` as `user_id` to all analytics/DB calls

**D5. Login page** (`app/templates/login.html`) — extends `base.html`; shows app name + "Sign in with Google" button; no nav bar shown when not authenticated

---

### Phase E — CSRF Protection

```python
# requirements.txt: starlette-csrf
from starlette_csrf import CSRFMiddleware
app.add_middleware(CSRFMiddleware, secret=os.environ["SECRET_KEY"])
```

Test all existing POST forms still work after enabling. No template changes needed.

---

### Phase F — Rate Limiting on Upload

```python
# requirements.txt: slowapi
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# On upload route:
@limiter.limit("10/hour")
async def upload_statement(request: Request, ...):
```

---

### Phase G — PostgreSQL

**G1. `app/database.py`** — read `DATABASE_URL` from env; fix Render's `postgres://` prefix:
```python
import os
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./finance_tracker.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
engine = create_engine(DATABASE_URL)
```

**G2. `alembic/env.py`** — ensure it reads `DATABASE_URL` from env (not hardcoded path)

**G3. Alembic batch migrations** — `batch_alter_table` is SQLite-specific. Existing migrations use it. For Postgres, use regular `op.add_column` etc. Claude to make new migrations Postgres-compatible by checking dialect or using non-batch ops for new migrations going forward.

**G4. One-time data migration** (`scripts/migrate_sqlite_to_postgres.py`)
- Connect to both DBs via SQLAlchemy
- Copy rows in dependency order: users → categories → statements → transactions → categorization_examples → portfolio tables
- Verify row counts match
- Run locally once before switching to Postgres permanently

---

### Phase H — PDF Deletion After Parse

In `app/routers/upload.py`, after successful `db.commit()` of the parsed statement:
```python
try:
    save_path.unlink(missing_ok=True)
except OSError:
    pass
```

Remove the PDF glob-cleanup block from `delete_statement()` — files are already gone post-parse.
The "Extract Balance" button is unaffected — it uses `statement.raw_text` from the DB.

---

### Phase I — Basic Tests

**Setup:** create `requirements-dev.txt`:
```
pytest
httpx
pytest-asyncio
```

**Tests to write** (`tests/` folder):
- `test_smoke.py` — GET `/dashboard`, `/upload`, `/review`, `/transactions`, `/balances` all return 200 (with mocked session)
- `test_hash.py` — `compute_transaction_hash` returns same hash for same inputs; different hashes with vs without `reference_id`
- `test_dedup.py` — same transaction twice → skipped=1; two transactions with different `reference_id` → both inserted

---

### Phase J — CI/CD (GitHub Actions)

Create `.github/workflows/ci.yml`:
```yaml
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: pip install -r requirements.txt -r requirements-dev.txt
      - run: pytest tests/
  deploy:
    needs: test
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    steps:
      - name: Trigger Render deploy
        run: curl -X POST "${{ secrets.RENDER_DEPLOY_HOOK_URL }}"
```

In Render: Settings → Deploy Hook → copy URL → add to GitHub repo Secrets as `RENDER_DEPLOY_HOOK_URL`.

---

### Phase K — Final Deployment Checklist

Before going live, confirm all of the following:
- [ ] `ENVIRONMENT=production` set in Render env vars
- [ ] `debug=False` confirmed (handled by `ENVIRONMENT` check in `app/main.py`)
- [ ] All secrets in Render env vars — nothing hardcoded
- [ ] Google OAuth redirect URI updated with Render URL
- [ ] Alembic migrations ran successfully against Postgres (`alembic upgrade head`)
- [ ] Data migrated from SQLite if carrying over dev data (`scripts/migrate_sqlite_to_postgres.py`)
- [ ] All routes smoke-tested on production URL
- [ ] **Enable GitHub Dependabot alerts** (Settings → Security → Dependabot alerts → Enable)
- [ ] OpenAI key rotated — fresh key for production; revoke the dev key in OpenAI dashboard
- [ ] `| safe` audit completed on all templates

---

## Deferred (after deployment is stable)

- Phase 6: Portfolio tracking (manual trade entry + yfinance prices)
- Phase 8–10: Live portfolio widgets, GPT insights, IBKR PDF parser
- Mobile app (React Native) — same FastAPI backend, new `/api/` JSON routes + JWT auth layer