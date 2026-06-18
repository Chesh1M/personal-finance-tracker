import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
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


@app.get("/health")
def health_check():
    return {"status": "ok"}
