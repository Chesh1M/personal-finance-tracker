import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.middleware.sessions import SessionMiddleware

from app.dependencies import RequiresLoginException
from app.limiter import limiter

BASE_DIR = Path(__file__).resolve().parent

_is_production = os.getenv("ENVIRONMENT", "development") == "production"
_secret_key = os.getenv("SECRET_KEY", "dev-secret-key-CHANGE-IN-PRODUCTION")

app = FastAPI(
    title="Personal Finance Tracker",
    debug=not _is_production,
)

# Dev tip: start with --reload-dir app to avoid venv triggering infinite reloads:
#   uvicorn app.main:app --reload --reload-dir app

# ── Rate limiting ────────────────────────────────────────────────────────────
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── Auth redirect handler ────────────────────────────────────────────────────
@app.exception_handler(RequiresLoginException)
async def requires_login_handler(request: Request, exc: RequiresLoginException):
    return RedirectResponse(url="/login", status_code=302)

# ── Session middleware (SameSite=Lax provides CSRF protection for form POSTs) ─
# starlette-csrf (double-submit cookie pattern) is installed but not active yet;
# it requires converting all form submissions to fetch() with X-CSRFToken header.
# TODO: enable CSRFMiddleware after adding JS CSRF injection to base.html.
app.add_middleware(SessionMiddleware, secret_key=_secret_key)

app.mount("/static", StaticFiles(directory=BASE_DIR.parent / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

from app.routers import auth, balances, transactions, upload, dashboard
app.include_router(auth.router)
app.include_router(upload.router)
app.include_router(transactions.router)
app.include_router(dashboard.router)
app.include_router(balances.router)

# Routers added as phases are completed:
# from app.routers import portfolio
# app.include_router(portfolio.router)


import logging as _logging
_unhandled_logger = _logging.getLogger("app.unhandled")

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Last-resort handler so unhandled exceptions never show a blank page."""
    _unhandled_logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    is_production = os.getenv("ENVIRONMENT", "development") == "production"
    detail = "An unexpected error occurred. Please try again." if is_production else str(exc)
    html = f"""<!doctype html><html lang="en">
<head><meta charset="utf-8"><title>Error — Finance Tracker</title>
<style>
  body{{margin:0;background:#09090b;color:#fafafa;font-family:'IBM Plex Sans',system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;}}
  .card{{background:#111113;border:1px solid #27272a;border-radius:12px;padding:2rem 2.5rem;max-width:480px;text-align:center;}}
  h1{{color:#ef4444;font-size:1.25rem;margin:0 0 0.75rem;}}
  p{{color:#a1a1aa;margin:0 0 1.5rem;font-size:0.9rem;line-height:1.6;}}
  a{{color:#0C5CAB;text-decoration:none;font-size:0.9rem;}}a:hover{{text-decoration:underline;}}
</style></head>
<body><div class="card">
  <h1>Something went wrong</h1>
  <p>{detail}</p>
  <a href="/">&larr; Back to Dashboard</a>
</div></body></html>"""
    return HTMLResponse(content=html, status_code=500)


@app.get("/health")
def health_check():
    return {"status": "ok"}
