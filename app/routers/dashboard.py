import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from openai import OpenAI
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models import AiMonthlyInsight, Category, Transaction, User
from app.services import analytics

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dashboard"])

templates = Jinja2Templates(
    directory=Path(__file__).resolve().parent.parent / "templates"
)


def _pct_change(curr: float, prev: float) -> float | None:
    """Percentage change from prev to curr. Returns None if prev is zero."""
    if not prev:
        return None
    return (curr - prev) / abs(prev) * 100


@router.post("/api/ask")
async def api_ask(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """AI data assistant — answer a free-text question about the user's finances.

    Request body: {"question": str, "selected_month": "YYYY-MM"}
    Response: {"answer_type": "text"|"chart", "text": str, "chart": {...}|null}
    """
    body = await request.json()
    question = str(body.get("question", "")).strip()
    selected_month_str = str(body.get("selected_month", ""))

    if not question:
        return JSONResponse({"error": "question required"}, status_code=400)

    user_id = current_user.id

    # Resolve anchor month
    try:
        anchor = date.fromisoformat(selected_month_str + "-01")
        anchor_year, anchor_month = anchor.year, anchor.month
    except ValueError:
        avail = analytics.get_available_months(db, user_id)
        if avail:
            anchor_year, anchor_month = avail[0]
        else:
            return JSONResponse({"answer_type": "text", "text": "No transaction data found.", "chart": None})

    # Build 6-month context
    from app.services.analytics import _prev_months, _month_start, _month_end, _month_label
    months = _prev_months(anchor_year, anchor_month, 6)
    start  = _month_start(months[0][0], months[0][1])
    end    = _month_end(months[-1][0], months[-1][1])
    context_label = f"{_month_label(*months[0])} – {_month_label(*months[-1])}"

    tx_rows = (
        db.query(Transaction)
        .filter(
            Transaction.is_reviewed == True,   # noqa: E712
            Transaction.user_id == user_id,
            Transaction.date >= start,
            Transaction.date <= end,
        )
        .order_by(Transaction.date.desc())
        .limit(500)
        .all()
    )

    # Build category lookup
    cats = {c.id: c.display_name for c in db.query(Category).all()}

    tx_list = [
        {
            "date":        str(tx.date),
            "description": tx.description,
            "amount":      round(abs(tx.amount), 2),
            "type":        tx.type,
            "category":    cats.get(tx.category_id, "Uncategorized"),
        }
        for tx in tx_rows
    ]

    # Monthly summaries for context
    monthly_summaries = []
    for y, m in months:
        s = analytics.get_summary_stats(db, y, m, user_id)
        monthly_summaries.append({
            "month":        _month_label(y, m),
            "spending":     s["total_spending"],
            "income":       s["total_income"],
            "savings_rate": s["savings_rate"],
        })

    context_payload = {
        "context_months":    context_label,
        "monthly_summaries": monthly_summaries,
        "transactions":      tx_list,
    }

    # Parse and validate conversation history from client
    raw_history = body.get("history", [])
    safe_history: list[dict] = []
    if isinstance(raw_history, list):
        for msg in raw_history:
            if (
                isinstance(msg, dict)
                and msg.get("role") in ("user", "assistant")
                and isinstance(msg.get("content"), str)
            ):
                safe_history.append({"role": msg["role"], "content": msg["content"][:1200]})

    system_prompt = (
        "You are a personal finance assistant for a user in Singapore. "
        "Answer questions about their spending and income using ONLY the data provided. "
        "Be concise and specific — cite amounts and merchants where relevant. "
        "Remember context from earlier in the conversation when answering follow-up questions. "
        "When the question asks for a breakdown, comparison, or trend that is best shown visually, "
        "set answer_type to 'chart' and populate the chart field. Otherwise use 'text'. "
        "Always respond with valid JSON matching this schema exactly:\n"
        '{"answer_type": "text" | "chart", "text": "<answer>", '
        '"chart": {"type": "bar"|"line"|"doughnut", "title": "...", '
        '"labels": [...], "datasets": [{"label": "...", "data": [...]}]} | null}'
    )

    # Build messages: system → data context primer → conversation history → current question
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": f"Here is my financial data for context:\n{json.dumps(context_payload)}"},
        {"role": "assistant", "content": "Understood. I have your financial data and am ready to answer your questions."},
    ]
    # Prior turns (all history entries except the last, which is the current question)
    for msg in safe_history[:-1]:
        messages.append(msg)
    # Current question
    messages.append({"role": "user", "content": question})

    try:
        client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"), timeout=45.0)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=800,
        )
        result = json.loads(resp.choices[0].message.content)
        return JSONResponse(result)
    except Exception:
        logger.error("api_ask GPT call failed for user %d", user_id)
        return JSONResponse({
            "answer_type": "text",
            "text": "Sorry, I couldn't process your question right now. Please try again.",
            "chart": None,
        })


@router.post("/api/refresh-insight")
def api_refresh_insight(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete cached AI insight for a month and regenerate it.

    Query param: ?month=YYYY-MM
    Returns: {"ok": true, "insight": "..."}
    """
    month_str = request.query_params.get("month", "")
    try:
        parsed = date.fromisoformat(month_str + "-01")
        year, month_int = parsed.year, parsed.month
    except ValueError:
        return JSONResponse({"ok": False, "error": "bad month"}, status_code=400)

    user_id = current_user.id

    # Delete cached row
    existing = (
        db.query(AiMonthlyInsight)
        .filter_by(user_id=user_id, year=year, month=month_int)
        .first()
    )
    if existing:
        db.delete(existing)
        db.commit()

    # Regenerate
    new_insight = analytics.generate_spending_insight(year, month_int, user_id, db)
    try:
        db.add(AiMonthlyInsight(
            user_id=user_id, year=year, month=month_int,
            insight_text=new_insight,
            generated_at=datetime.now(timezone.utc),
        ))
        db.commit()
    except Exception:
        db.rollback()

    return JSONResponse({"ok": True, "insight": new_insight})


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
    try:
        income_breakdown = analytics.get_income_breakdown(db, year, month_int, user_id)
    except Exception:
        logger.exception("get_income_breakdown failed")
        db.rollback()
        income_breakdown = []
    try:
        category_trend = analytics.get_category_monthly_trend(db, year, month_int, user_id)
    except Exception:
        logger.exception("get_category_monthly_trend failed")
        db.rollback()
        category_trend = {"labels": [], "categories": {}}
    spending_categories = (
        db.query(Category)
        .filter(Category.is_transfer == False, Category.name != "reimbursements")  # noqa: E712
        .order_by(Category.display_name)
        .all()
    )

    # ── AI spending insight (read from cache only; generate via /api/refresh-insight) ──
    ai_insight: str | None = None
    if year and month_int:
        try:
            existing_insight = (
                db.query(AiMonthlyInsight)
                .filter_by(user_id=user_id, year=year, month=month_int)
                .first()
            )
            if existing_insight:
                ai_insight = existing_insight.insight_text
        except Exception:
            logger.exception("Failed to read AI insight from cache (table may not exist)")
            db.rollback()  # reset session so subsequent queries are not affected

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
            "income_breakdown_json":  json.dumps(income_breakdown),
            "category_trend_json":    json.dumps(category_trend),
            "ai_insight":             ai_insight,
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
