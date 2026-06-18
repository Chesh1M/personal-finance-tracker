import json
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models import Category, User
from app.services import analytics

router = APIRouter(tags=["dashboard"])

templates = Jinja2Templates(
    directory=Path(__file__).resolve().parent.parent / "templates"
)


def _pct_change(curr: float, prev: float) -> float | None:
    """Percentage change from prev to curr. Returns None if prev is zero."""
    if not prev:
        return None
    return (curr - prev) / abs(prev) * 100


@router.get("/api/spending")
def api_spending(
    request: Request,
    month: str | None = None,
    comparison_month: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return fresh category spending data and total_spending for live JS chart refresh."""
    year, month_int = None, None
    if month:
        try:
            parsed = date.fromisoformat(month + "-01")
            year, month_int = parsed.year, parsed.month
        except ValueError:
            return JSONResponse({"error": "bad month"}, status_code=400)

    user_id = current_user.id
    categories = analytics.get_spending_by_category(db, year, month_int, user_id)
    stats = analytics.get_summary_stats(db, year, month_int, user_id)
    trend = analytics.get_monthly_trend(db, year, month_int, user_id) if year and month_int else None

    # Comparison month data
    comp_year, comp_month_int = None, None
    if comparison_month:
        try:
            cp = date.fromisoformat(comparison_month + "-01")
            comp_year, comp_month_int = cp.year, cp.month
        except ValueError:
            pass
    comp_categories = analytics.get_spending_by_category(db, comp_year, comp_month_int, user_id) if comp_year else []
    comp_stats = analytics.get_summary_stats(db, comp_year, comp_month_int, user_id) if comp_year else None

    return JSONResponse({
        "categories":     categories,
        "total_spending": stats["total_spending"],
        "total_income":   stats["total_income"],
        "savings_rate":   stats["savings_rate"],
        "savings":        stats["savings"],
        "tx_count":       stats["tx_count"],
        "trend":          trend,
        # Comparison
        "comparison_categories":     comp_categories,
        "comparison_total_spending": comp_stats["total_spending"] if comp_stats else None,
        "comparison_total_income":   comp_stats["total_income"]   if comp_stats else None,
        "comparison_savings_rate":   comp_stats["savings_rate"]   if comp_stats else None,
        "comparison_savings":        comp_stats["savings"]        if comp_stats else None,
        "comparison_tx_count":       comp_stats["tx_count"]       if comp_stats else None,
    })


@router.get("/")
def root_redirect():
    """Redirect bare root to the dashboard page (nav links to '/')."""
    return RedirectResponse(url="/dashboard", status_code=302)


@router.get("/dashboard")
def dashboard(
    request: Request,
    month: str | None = None,               # "YYYY-MM" or "" for All Time
    comparison_month: str | None = None,    # "YYYY-MM" for comparison period
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Main spending analytics dashboard."""
    user_id = current_user.id
    available_months = analytics.get_available_months(db, user_id)

    # ── Empty state ────────────────────────────────────────────────────────
    if not available_months:
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "empty": True,
                "available_months": [],
                "selected_month": "",
                "comparison_month": "",
                "current_user": current_user,
            },
        )

    # ── Resolve selected (year, month_int) ─────────────────────────────────
    year: int | None = None
    month_int: int | None = None
    selected_month_str: str = ""

    if month == "" or month is None:
        # Default to the most recent month with data
        year, month_int = available_months[0]
        selected_month_str = f"{year}-{month_int:02d}"
    else:
        try:
            parsed = date.fromisoformat(month + "-01")
            year = parsed.year
            month_int = parsed.month
            selected_month_str = f"{year}-{month_int:02d}"
            if (year, month_int) not in available_months:
                pass  # Honour it anyway — charts show zeros
        except ValueError:
            return RedirectResponse(url="/dashboard", status_code=302)

    # ── Resolve comparison (year, month_int) ───────────────────────────────
    # Default: 1 month before primary
    if month_int == 1:
        comp_year_default, comp_month_default = year - 1, 12
    else:
        comp_year_default, comp_month_default = year, month_int - 1

    comp_year: int = comp_year_default
    comp_month_int: int = comp_month_default
    comparison_month_str: str = f"{comp_year}-{comp_month_int:02d}"

    if comparison_month:
        try:
            cp = date.fromisoformat(comparison_month + "-01")
            comp_year = cp.year
            comp_month_int = cp.month
            comparison_month_str = f"{comp_year}-{comp_month_int:02d}"
        except ValueError:
            pass  # keep default

    # ── Gather analytics data ──────────────────────────────────────────────
    stats            = analytics.get_summary_stats(db, year, month_int, user_id)
    category_data    = analytics.get_spending_by_category(db, year, month_int, user_id)
    category_details = analytics.get_category_transaction_details(db, year, month_int, user_id)
    trend_data       = analytics.get_monthly_trend(db, year, month_int, user_id)
    account_balances = analytics.get_account_balances(db, year, month_int, user_id)
    reimbursements   = analytics.get_reimbursements(db, year, month_int, user_id)
    insights         = analytics.get_insights(db, year, month_int, user_id)
    spending_categories = (
        db.query(Category)
        .filter(Category.is_transfer == False, Category.name != "reimbursements")  # noqa: E712
        .order_by(Category.display_name)
        .all()
    )

    # ── Gather comparison data ─────────────────────────────────────────────
    comparison_stats         = analytics.get_summary_stats(db, comp_year, comp_month_int, user_id)
    comparison_category_data = analytics.get_spending_by_category(db, comp_year, comp_month_int, user_id)
    comparison_balances      = analytics.get_account_balances(db, comp_year, comp_month_int, user_id)

    # Pre-compute deltas (keeps Jinja2 simple)
    comparison_deltas = {
        "total_spending_prev": comparison_stats["total_spending"],
        "total_spending_pct":  _pct_change(stats["total_spending"], comparison_stats["total_spending"]),
        "total_income_prev":   comparison_stats["total_income"],
        "total_income_pct":    _pct_change(stats["total_income"], comparison_stats["total_income"]),
        "savings_rate_prev":   comparison_stats["savings_rate"],
        "savings_rate_delta":  (
            (stats["savings_rate"] or 0.0) - (comparison_stats["savings_rate"] or 0.0)
            if stats["savings_rate"] is not None and comparison_stats["savings_rate"] is not None
            else None
        ),
        "tx_count_prev":  comparison_stats["tx_count"],
        "tx_count_delta": stats["tx_count"] - comparison_stats["tx_count"],
    }

    # Merge comparison balances into account rows
    comp_bal_lookup = {
        (a["bank_source"], a["account_type"]): a["closing_balance"]
        for a in comparison_balances["accounts"]
    }
    for acct in account_balances["accounts"]:
        prev = comp_bal_lookup.get((acct["bank_source"], acct["account_type"]))
        acct["comparison_balance"] = prev
        acct["comparison_balance_pct"] = (
            _pct_change(acct["closing_balance"], prev) if prev is not None else None
        )

    # Net total comparison
    account_balances["comparison_net_total"] = (
        comparison_balances["net_total"] if comparison_balances["has_data"] else None
    )
    account_balances["comparison_net_total_pct"] = (
        _pct_change(account_balances["net_total"], comparison_balances["net_total"])
        if comparison_balances["has_data"] and comparison_balances["net_total"] != 0
        else None
    )

    # ── Build month dropdown labels ────────────────────────────────────────
    import calendar as _cal
    month_options = [
        {
            "value": f"{y}-{m:02d}",
            "label": f"{_cal.month_abbr[m]} {y}",
        }
        for y, m in available_months
    ]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "empty": False,
            "stats": stats,
            "category_json":          json.dumps(category_data),
            "category_details_json":  json.dumps(category_details),
            "trend_json":             json.dumps(trend_data),
            "account_balances":       account_balances,
            "reimbursements":         reimbursements,
            "spending_categories":    spending_categories,
            "insights":               insights,
            "month_options":          month_options,
            "selected_month":         selected_month_str,
            # Comparison
            "comparison_month":       comparison_month_str,
            "comparison_stats":       comparison_stats,
            "comparison_deltas":      comparison_deltas,
            "comparison_category_json": json.dumps(comparison_category_data),
            "current_user":           current_user,
        },
    )
