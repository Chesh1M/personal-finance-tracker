"""
Analytics service — Phase 7 (spending dashboard).
All functions query only is_reviewed=True transactions scoped to a user_id.
Amounts are always abs() — the `type` field carries sign semantics.
No python-dateutil: month arithmetic uses the calendar stdlib.
"""

import calendar
from datetime import date

from sqlalchemy import func
from sqlalchemy.orm import Session, aliased

from app.models import Category, StatementUpload, Transaction


# ── Split-range helper ──────────────────────────────────────────────────────

def _months_in_range(start_ym: str, end_ym: str) -> int:
    """Number of calendar months between two YYYY-MM strings, inclusive."""
    sy, sm = int(start_ym[:4]), int(start_ym[5:7])
    ey, em = int(end_ym[:4]), int(end_ym[5:7])
    return max(1, (ey - sy) * 12 + (em - sm) + 1)


# ── Month arithmetic helpers ────────────────────────────────────────────────

def _month_start(year: int, month: int) -> date:
    return date(year, month, 1)


def _month_end(year: int, month: int) -> date:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, last_day)


def _prev_months(year: int, month: int, n: int) -> list[tuple[int, int]]:
    """Return list of (year, month) tuples going n months back, oldest first.
    Includes the anchor (year, month) itself as the last element."""
    result = []
    y, m = year, month
    for _ in range(n):
        result.append((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(result))


def _prev_month(year: int, month: int) -> tuple[int, int]:
    """Return (year, month) for the month immediately before the given one."""
    m = month - 1
    y = year
    if m == 0:
        m = 12
        y -= 1
    return y, m


def _month_label(year: int, month: int) -> str:
    return f"{calendar.month_abbr[month]} {year}"


# ── Shared date-range filter helper ────────────────────────────────────────

def _date_range_filter(year: int | None, month: int | None):
    """Return a list of SQLAlchemy filter conditions for the posting date range.
    Uses Transaction.date (posting/statement date) — consistent with how
    get_available_months buckets months and with how users think about statement months.
    If year/month are None, returns empty list (= "All Time").
    """
    if year is None or month is None:
        return []
    start = _month_start(year, month)
    end = _month_end(year, month)
    return [Transaction.date >= start, Transaction.date <= end]


# ── Public API ──────────────────────────────────────────────────────────────

def get_available_months(db: Session, user_id: int) -> list[tuple[int, int]]:
    """Return distinct (year, month) tuples that have reviewed transactions,
    sorted descending (most recent first). Returns [] if no data."""
    rows = (
        db.query(
            func.strftime("%Y", Transaction.date).label("yr"),
            func.strftime("%m", Transaction.date).label("mo"),
        )
        .filter(
            Transaction.is_reviewed == True,  # noqa: E712
            Transaction.user_id == user_id,
        )
        .distinct()
        .all()
    )
    pairs = sorted(
        {(int(r.yr), int(r.mo)) for r in rows},
        reverse=True,
    )
    return pairs


def get_summary_stats(db: Session, year: int | None, month: int | None, user_id: int) -> dict:
    """Summary card numbers for the selected period (or all time if year/month are None).

    Returns:
        {
            "total_spending": float,
            "total_income":   float,
            "savings":        float,
            "savings_rate":   float | None,   # None when income == 0
            "tx_count":       int,
            "period_label":   str,            # e.g. "May 2026" or "All Time"
        }
    """
    base = [
        Transaction.is_reviewed == True,  # noqa: E712
        Transaction.is_transfer == False,  # noqa: E712
        Transaction.user_id == user_id,
    ]
    date_f = _date_range_filter(year, month)

    total_spending = (
        db.query(func.sum(func.abs(Transaction.amount)))
        .filter(*base, *date_f, Transaction.type == "debit")
        .scalar()
        or 0.0
    )
    income_cat = db.query(Category).filter(Category.name == "income").first()
    income_cat_id = income_cat.id if income_cat else None

    if income_cat_id:
        total_income = (
            db.query(func.sum(func.abs(Transaction.amount)))
            .filter(*base, *date_f,
                    Transaction.type == "credit",
                    Transaction.category_id == income_cat_id)
            .scalar()
            or 0.0
        )
    else:
        total_income = 0.0

    # Deduct ALL reimbursements from total spending (tagged or not; split or not)
    reimb_cat = db.query(Category).filter(Category.name == "reimbursements").first()
    if reimb_cat:
        current_ym = f"{year:04d}-{month:02d}" if year and month else None

        if current_ym:
            # Specific month — non-split reimbursements that land in this period
            non_split_total = (
                db.query(func.sum(func.abs(Transaction.amount)))
                .filter(
                    *base, *date_f,
                    Transaction.type == "credit",
                    Transaction.category_id == reimb_cat.id,
                    Transaction.split_start_month.is_(None),
                )
                .scalar()
                or 0.0
            )
            # Split reimbursements whose range covers this month
            split_rows = (
                db.query(
                    Transaction.amount,
                    Transaction.split_start_month,
                    Transaction.split_end_month,
                )
                .filter(
                    Transaction.is_reviewed == True,   # noqa: E712
                    Transaction.is_transfer == False,  # noqa: E712
                    Transaction.user_id == user_id,
                    Transaction.type == "credit",
                    Transaction.category_id == reimb_cat.id,
                    Transaction.split_start_month.isnot(None),
                    Transaction.split_start_month <= current_ym,
                    Transaction.split_end_month   >= current_ym,
                )
                .all()
            )
            split_total = sum(
                abs(r.amount) / _months_in_range(r.split_start_month, r.split_end_month)
                for r in split_rows
            )
            total_spending = max(0.0, total_spending - non_split_total - split_total)
        else:
            # All Time — deduct full amount of every reimbursement
            all_reimb = (
                db.query(func.sum(func.abs(Transaction.amount)))
                .filter(
                    *base,
                    Transaction.type == "credit",
                    Transaction.category_id == reimb_cat.id,
                )
                .scalar()
                or 0.0
            )
            total_spending = max(0.0, total_spending - all_reimb)

    tx_count = (
        db.query(func.count(Transaction.id))
        .filter(*base, *date_f)
        .scalar()
        or 0
    )

    savings = total_income - total_spending
    savings_rate = round(savings / total_income * 100, 1) if total_income > 0 else None

    if year and month:
        label = _month_label(year, month)
    else:
        label = "All Time"

    return {
        "total_spending": round(total_spending, 2),
        "total_income": round(total_income, 2),
        "savings": round(savings, 2),
        "savings_rate": savings_rate,
        "tx_count": tx_count,
        "period_label": label,
    }


def get_spending_by_category(
    db: Session, year: int | None, month: int | None, user_id: int
) -> list[dict]:
    """Sum of reviewed debit spending grouped by category for the selected period.

    Returns list sorted descending by amount:
        [{"category": "Food & Dining", "amount": 450.20, "pct": 35.2}, ...]
    Transactions with no category appear as "Uncategorized".
    """
    base = [
        Transaction.is_reviewed == True,  # noqa: E712
        Transaction.is_transfer == False,  # noqa: E712
        Transaction.type == "debit",
        Transaction.user_id == user_id,
    ]
    date_f = _date_range_filter(year, month)

    rows = (
        db.query(
            func.coalesce(Category.display_name, "Uncategorized").label("cat"),
            func.sum(func.abs(Transaction.amount)).label("total"),
            func.count(Transaction.id).label("count"),
        )
        .outerjoin(Category, Transaction.category_id == Category.id)
        .filter(*base, *date_f)
        .group_by(func.coalesce(Category.display_name, "Uncategorized"))
        .order_by(func.sum(func.abs(Transaction.amount)).desc())
        .all()
    )

    # Compute tagged-reimbursement deductions per spending category
    reimb_cat = db.query(Category).filter(Category.name == "reimbursements").first()
    deductions: dict[str, float] = {}
    if reimb_cat:
        tag_alias = aliased(Category)
        current_ym = f"{year:04d}-{month:02d}" if year and month else None

        # Non-split: reimbursement transaction date falls in the current month
        non_split_rows = (
            db.query(
                tag_alias.display_name.label("cat"),
                func.sum(func.abs(Transaction.amount)).label("total"),
            )
            .join(tag_alias, Transaction.reimbursement_category_id == tag_alias.id)
            .filter(
                Transaction.is_reviewed == True,   # noqa: E712
                Transaction.is_transfer == False,  # noqa: E712
                Transaction.user_id == user_id,
                Transaction.category_id == reimb_cat.id,
                Transaction.reimbursement_category_id.isnot(None),
                Transaction.split_start_month.is_(None),
                *date_f,
            )
            .group_by(tag_alias.display_name)
            .all()
        )
        deductions = {r.cat: (r.total or 0.0) for r in non_split_rows}

        # Split: transaction may be in any month; distribute amount / n_months
        if current_ym:
            split_rows = (
                db.query(
                    tag_alias.display_name.label("cat"),
                    Transaction.amount,
                    Transaction.split_start_month,
                    Transaction.split_end_month,
                )
                .join(tag_alias, Transaction.reimbursement_category_id == tag_alias.id)
                .filter(
                    Transaction.is_reviewed == True,   # noqa: E712
                    Transaction.is_transfer == False,  # noqa: E712
                    Transaction.user_id == user_id,
                    Transaction.category_id == reimb_cat.id,
                    Transaction.reimbursement_category_id.isnot(None),
                    Transaction.split_start_month.isnot(None),
                    Transaction.split_start_month <= current_ym,
                    Transaction.split_end_month   >= current_ym,
                )
                .all()
            )
            for r in split_rows:
                n = _months_in_range(r.split_start_month, r.split_end_month)
                per_month = abs(r.amount) / n
                deductions[r.cat] = deductions.get(r.cat, 0.0) + per_month

    result = []
    for r in rows:
        gross = r.total or 0.0
        net = max(0.0, round(gross - deductions.get(r.cat, 0.0), 2))
        if net == 0.0:
            continue
        effective_deduction = round(gross - net, 2)
        result.append({
            "category":  r.cat,
            "amount":    net,
            "pct":       0.0,
            "tx_count":  r.count,
            "deduction": effective_deduction,
        })

    # Re-sort by net amount (gross sort may no longer hold after deductions)
    result.sort(key=lambda d: d["amount"], reverse=True)

    grand_total = sum(d["amount"] for d in result)
    for d in result:
        d["pct"] = round(d["amount"] / grand_total * 100, 1) if grand_total > 0 else 0.0
    return result


def get_category_transaction_details(
    db: Session, year: int | None, month: int | None, user_id: int
) -> dict[str, list[dict]]:
    """Returns {category_display_name: [{description, amount}, ...]} for all
    reviewed debit (non-transfer) transactions in the period, sorted by amount
    desc within each category. Used for bar-chart tooltip drill-down."""
    base = [
        Transaction.is_reviewed == True,   # noqa: E712
        Transaction.is_transfer == False,  # noqa: E712
        Transaction.type == "debit",
        Transaction.user_id == user_id,
    ]
    date_f = _date_range_filter(year, month)
    rows = (
        db.query(
            func.coalesce(Category.display_name, "Uncategorized").label("cat"),
            Transaction.description,
            func.abs(Transaction.amount).label("amount"),
        )
        .outerjoin(Category, Transaction.category_id == Category.id)
        .filter(*base, *date_f)
        .order_by(
            func.coalesce(Category.display_name, "Uncategorized"),
            func.abs(Transaction.amount).desc(),
        )
        .all()
    )
    result: dict[str, list[dict]] = {}
    for r in rows:
        cat = r.cat
        if cat not in result:
            result[cat] = []
        result[cat].append({
            "description": r.description,
            "amount": round(float(r.amount), 2),
        })
    return result


def get_account_balances(db: Session, year: int | None, month: int | None, user_id: int) -> dict:
    """Returns closing-balance data for all statements that have transactions
    posting in the selected month (year/month required; returns empty for All Time).

    Returns:
        {
            "accounts": [
                {
                    "statement_id": int,
                    "bank_source":  str,
                    "account_type": str,        # "savings" | "credit_card" | …
                    "closing_balance": float,
                    "is_liability": bool,       # True for credit_card
                },
                ...
            ],
            "net_total": float,   # sum(assets) - sum(liabilities)
            "has_data":  bool,
        }
    """
    if year is None or month is None:
        return {"accounts": [], "net_total": 0.0, "has_data": False}

    start = _month_start(year, month)
    end   = _month_end(year, month)

    rows = (
        db.query(StatementUpload)
        .join(Transaction, Transaction.statement_id == StatementUpload.id)
        .filter(
            StatementUpload.user_id == user_id,
            Transaction.date >= start,
            Transaction.date <= end,
            StatementUpload.closing_balance.isnot(None),
        )
        .distinct()
        .all()
    )

    accounts = []
    seen_ids: set[int] = set()
    for su in rows:
        if su.id in seen_ids:
            continue
        seen_ids.add(su.id)
        is_liability = su.account_type == "credit_card"
        accounts.append({
            "statement_id":    su.id,
            "bank_source":     su.bank_source,
            "account_type":    su.account_type or "other",
            "closing_balance": round(su.closing_balance, 2),
            "is_liability":    is_liability,
        })

    net_total = sum(
        (-a["closing_balance"] if a["is_liability"] else a["closing_balance"])
        for a in accounts
    )

    return {
        "accounts": accounts,
        "net_total": round(net_total, 2),
        "has_data":  len(accounts) > 0,
    }


def get_monthly_trend(
    db: Session, year: int, month: int, user_id: int, n_months: int = 12
) -> dict:
    """Monthly spending and income for the last n_months months ending at (year, month).

    Returns:
        {
            "labels":   ["Jun 2025", ..., "May 2026"],
            "spending": [1200.50, ...],
            "income":   [3500.00, ...],
        }
    """
    months = _prev_months(year, month, n_months)
    labels, spending_vals, income_vals = [], [], []

    base = [
        Transaction.is_reviewed == True,  # noqa: E712
        Transaction.is_transfer == False,  # noqa: E712
        Transaction.user_id == user_id,
    ]

    income_cat = db.query(Category).filter(Category.name == "income").first()
    income_cat_id = income_cat.id if income_cat else None
    reimb_cat = db.query(Category).filter(Category.name == "reimbursements").first()

    for y, m in months:
        start, end = _month_start(y, m), _month_end(y, m)
        date_f = [Transaction.date >= start, Transaction.date <= end]
        current_ym = f"{y:04d}-{m:02d}"

        spending = (
            db.query(func.sum(func.abs(Transaction.amount)))
            .filter(*base, *date_f, Transaction.type == "debit")
            .scalar()
            or 0.0
        )

        # Deduct ALL reimbursements for this month (tagged or not)
        if reimb_cat:
            non_split_reimb = (
                db.query(func.sum(func.abs(Transaction.amount)))
                .filter(
                    *base, *date_f,
                    Transaction.type == "credit",
                    Transaction.category_id == reimb_cat.id,
                    Transaction.split_start_month.is_(None),
                )
                .scalar()
                or 0.0
            )
            split_rows = (
                db.query(
                    Transaction.amount,
                    Transaction.split_start_month,
                    Transaction.split_end_month,
                )
                .filter(
                    Transaction.is_reviewed == True,   # noqa: E712
                    Transaction.is_transfer == False,  # noqa: E712
                    Transaction.user_id == user_id,
                    Transaction.type == "credit",
                    Transaction.category_id == reimb_cat.id,
                    Transaction.split_start_month.isnot(None),
                    Transaction.split_start_month <= current_ym,
                    Transaction.split_end_month   >= current_ym,
                )
                .all()
            )
            split_reimb = sum(
                abs(r.amount) / _months_in_range(r.split_start_month, r.split_end_month)
                for r in split_rows
            )
            spending = max(0.0, spending - non_split_reimb - split_reimb)

        if income_cat_id:
            income = (
                db.query(func.sum(func.abs(Transaction.amount)))
                .filter(*base, *date_f, Transaction.type == "credit",
                        Transaction.category_id == income_cat_id)
                .scalar()
                or 0.0
            )
        else:
            income = 0.0
        labels.append(_month_label(y, m))
        spending_vals.append(round(spending, 2))
        income_vals.append(round(income, 2))

    return {"labels": labels, "spending": spending_vals, "income": income_vals}


def get_income_vs_expenses(
    db: Session, year: int, month: int, user_id: int, n_months: int = 6
) -> dict:
    """Monthly income vs expenses for the last n_months months ending at (year, month).

    Returns:
        {
            "labels":   ["Dec 2025", ..., "May 2026"],
            "expenses": [1200.50, ...],
            "income":   [3500.00, ...],
        }
    """
    trend = get_monthly_trend(db, year, month, user_id, n_months=n_months)
    return {
        "labels": trend["labels"],
        "expenses": trend["spending"],
        "income": trend["income"],
    }


def get_reimbursements(db: Session, year: int | None, month: int | None, user_id: int) -> dict:
    """Reimbursement credit transactions for the selected period.

    Returns:
        {
            "total": float,
            "transactions": [{"description": str, "amount": float, "date": str}, ...],
            "has_data": bool,
        }
    """
    reimb_cat = db.query(Category).filter(Category.name == "reimbursements").first()
    if not reimb_cat:
        return {"total": 0.0, "transactions": [], "has_data": False}

    date_f = _date_range_filter(year, month)
    tag_alias = aliased(Category)
    rows = (
        db.query(
            Transaction.id,
            Transaction.description,
            Transaction.amount,
            Transaction.date,
            Transaction.reimbursement_category_id,
            Transaction.split_start_month,
            Transaction.split_end_month,
            tag_alias.display_name.label("tag_name"),
        )
        .outerjoin(tag_alias, Transaction.reimbursement_category_id == tag_alias.id)
        .filter(
            Transaction.is_reviewed == True,   # noqa: E712
            Transaction.is_transfer == False,  # noqa: E712
            Transaction.user_id == user_id,
            Transaction.type == "credit",
            Transaction.category_id == reimb_cat.id,
            *date_f,
        )
        .order_by(Transaction.date.desc())
        .all()
    )

    total = round(sum(abs(r.amount) for r in rows), 2)
    txns = [
        {
            "id": r.id,
            "description": r.description,
            "amount": round(abs(r.amount), 2),
            "date": r.date.strftime("%d %b %Y"),
            "reimbursement_category_id": r.reimbursement_category_id,
            "split_start_month": r.split_start_month,
            "split_end_month":   r.split_end_month,
            "tag_name": r.tag_name,
        }
        for r in rows
    ]
    return {"total": total, "transactions": txns, "has_data": len(rows) > 0}


def get_insights(db: Session, year: int | None, month: int | None, user_id: int) -> list[dict]:
    """Rule-based insight cards. Returns at most 5 cards.

    Each card: {"type": "good" | "warn", "title": str, "body": str}

    Only produces insights when a specific month is selected (not "All Time"),
    since comparisons require a defined reference period.
    """
    if year is None or month is None:
        return []

    cards: list[dict] = []

    curr = get_summary_stats(db, year, month, user_id)
    py, pm = _prev_month(year, month)
    prev = get_summary_stats(db, py, pm, user_id)

    label = curr["period_label"]
    prev_label = prev["period_label"]

    # 1. Savings rate
    sr = curr["savings_rate"]
    if sr is not None:
        if sr < 0:
            cards.append({
                "type": "warn",
                "title": "Spending exceeded income",
                "body": f"You spent more than you earned in {label}.",
            })
        elif sr < 10:
            cards.append({
                "type": "warn",
                "title": "Low savings rate",
                "body": f"Saved only {sr:.0f}% of income in {label}.",
            })
        elif sr >= 20:
            cards.append({
                "type": "good",
                "title": "Strong savings rate",
                "body": f"You saved {sr:.0f}% of income in {label} — great work!",
            })

    # 2. Month-over-month spending delta
    prev_spend = prev["total_spending"]
    curr_spend = curr["total_spending"]
    if prev_spend > 0 and curr_spend > 0:
        delta_pct = (curr_spend - prev_spend) / prev_spend * 100
        if delta_pct > 20:
            cards.append({
                "type": "warn",
                "title": "Spending spike",
                "body": f"Spending is up {delta_pct:.0f}% vs {prev_label} (${curr_spend:,.0f} vs ${prev_spend:,.0f}).",
            })
        elif delta_pct < -10:
            cards.append({
                "type": "good",
                "title": "Spending down",
                "body": f"You spent {abs(delta_pct):.0f}% less than {prev_label} (${curr_spend:,.0f} vs ${prev_spend:,.0f}).",
            })

    # 3. Top category spike
    curr_cats = {d["category"]: d["amount"] for d in get_spending_by_category(db, year, month, user_id)}
    prev_cats = {d["category"]: d["amount"] for d in get_spending_by_category(db, py, pm, user_id)}
    for cat, amount in curr_cats.items():
        if cat == "Uncategorized":
            continue
        prev_amount = prev_cats.get(cat, 0)
        if prev_amount > 0 and amount >= 50:
            spike_pct = (amount - prev_amount) / prev_amount * 100
            if spike_pct > 50:
                cards.append({
                    "type": "warn",
                    "title": f"Spike: {cat}",
                    "body": f"{cat} jumped {spike_pct:.0f}% vs {prev_label} (${amount:,.0f} vs ${prev_amount:,.0f}).",
                })
                break  # limit to one category spike card

    # 4. Uncategorized transactions
    date_f = _date_range_filter(year, month)
    uncat_count = (
        db.query(func.count(Transaction.id))
        .filter(
            Transaction.is_reviewed == True,  # noqa: E712
            Transaction.is_transfer == False,  # noqa: E712
            Transaction.user_id == user_id,
            Transaction.category_id == None,  # noqa: E711
            *date_f,
        )
        .scalar()
        or 0
    )
    if uncat_count > 0:
        cards.append({
            "type": "warn",
            "title": "Uncategorized transactions",
            "body": f"{uncat_count} transaction{'s' if uncat_count != 1 else ''} in {label} {'have' if uncat_count != 1 else 'has'} no category — assign on the Review page.",
        })

    # 5. No income detected
    if curr["total_income"] == 0 and curr["total_spending"] > 0:
        cards.append({
            "type": "warn",
            "title": "No income recorded",
            "body": f"No credit transactions found for {label}. Upload your income statement if missing.",
        })

    return cards[:5]
