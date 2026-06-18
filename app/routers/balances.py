"""
Balances router — account closing-balance management page.

GET  /balances            — table of all completed statements with editable
                            Account Type + Closing Balance fields
POST /balances/update     — AJAX: update closing_balance + account_type for
                            a given statement_id; returns {"ok": true}
"""
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models import StatementUpload, Transaction, User

router = APIRouter(tags=["balances"])

templates = Jinja2Templates(
    directory=Path(__file__).resolve().parent.parent / "templates"
)

ACCOUNT_TYPES = ["savings", "current", "credit_card", "paylah", "other"]


@router.get("/balances")
def balances_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Show all completed statements with their closing balances, editable inline."""
    rows = (
        db.query(
            StatementUpload,
            func.min(Transaction.date).label("period_start"),
            func.max(Transaction.date).label("period_end"),
        )
        .join(Transaction, Transaction.statement_id == StatementUpload.id)
        .filter(
            StatementUpload.status == "completed",
            StatementUpload.user_id == current_user.id,
        )
        .group_by(StatementUpload.id)
        .order_by(StatementUpload.bank_source, func.min(Transaction.date).desc())
        .all()
    )

    statements = [
        {
            "id":              row.StatementUpload.id,
            "bank_source":     row.StatementUpload.bank_source,
            "account_type":    row.StatementUpload.account_type or "",
            "closing_balance": row.StatementUpload.closing_balance,
            "period_start":    row.period_start,
            "period_end":      row.period_end,
            "filename":        row.StatementUpload.filename,
        }
        for row in rows
    ]

    return templates.TemplateResponse(request, "balances.html", {
        "statements":   statements,
        "account_types": ACCOUNT_TYPES,
        "current_user": current_user,
    })


@router.post("/balances/update")
async def update_balance(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """AJAX endpoint: update closing_balance and/or account_type for a statement."""
    form = await request.form()
    statement_id = form.get("statement_id")
    if not statement_id:
        return JSONResponse({"ok": False, "error": "missing statement_id"}, status_code=400)

    statement = db.query(StatementUpload).filter(
        StatementUpload.id == int(statement_id),
        StatementUpload.user_id == current_user.id,
    ).first()
    if not statement:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)

    # Update closing_balance if provided
    raw_balance = form.get("closing_balance")
    if raw_balance is not None and str(raw_balance).strip() != "":
        try:
            statement.closing_balance = float(raw_balance)
        except ValueError:
            return JSONResponse({"ok": False, "error": "invalid closing_balance"}, status_code=400)
    elif raw_balance is not None and str(raw_balance).strip() == "":
        statement.closing_balance = None   # allow clearing

    # Update account_type if provided
    raw_type = form.get("account_type")
    if raw_type is not None:
        statement.account_type = str(raw_type).strip() or None

    db.commit()
    return JSONResponse({"ok": True})
