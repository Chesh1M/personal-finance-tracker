from datetime import date as date_type, datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
import hashlib

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models import CategorizationExample, Category, Transaction, User

router = APIRouter(tags=["transactions"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")


@router.get("/review")
def review_page(
    request: Request,
    new: int = 0,
    inserted: int = 0,
    skipped: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    transactions = (
        db.query(Transaction)
        .filter(Transaction.is_reviewed == False, Transaction.user_id == current_user.id)
        .order_by(Transaction.date.desc())
        .all()
    )
    categories = db.query(Category).order_by(Category.display_name).all()
    return templates.TemplateResponse(request, "review.html", {
        "transactions": transactions,
        "categories": categories,
        "new": new,
        "inserted": inserted,
        "skipped": skipped,
        "current_user": current_user,
    })


_PAGE_SIZE = 50


@router.get("/transactions")
def transactions_view(
    request: Request,
    q: str = "",
    type: str = "",
    category_id: str = "",
    account: str = "",
    from_date: str = "",
    to_date: str = "",
    is_transfer: str = "",
    reviewed: str = "",
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Browse ALL transactions with filtering and pagination."""

    # ── Build filter conditions ────────────────────────────────────────────
    conditions = [Transaction.user_id == current_user.id]

    if q.strip():
        conditions.append(Transaction.description.ilike(f"%{q.strip()}%"))

    if type in ("debit", "credit"):
        conditions.append(Transaction.type == type)

    if category_id == "0":
        conditions.append(Transaction.category_id == None)  # noqa: E711
    elif category_id.strip() and category_id.isdigit():
        conditions.append(Transaction.category_id == int(category_id))

    if account.strip():
        conditions.append(Transaction.account_type == account)

    if from_date.strip():
        try:
            conditions.append(Transaction.date >= date_type.fromisoformat(from_date))
        except ValueError:
            pass

    if to_date.strip():
        try:
            conditions.append(Transaction.date <= date_type.fromisoformat(to_date))
        except ValueError:
            pass

    if is_transfer == "1":
        conditions.append(Transaction.is_transfer == True)   # noqa: E712
    elif is_transfer == "0":
        conditions.append(Transaction.is_transfer == False)  # noqa: E712

    if reviewed == "1":
        conditions.append(Transaction.is_reviewed == True)   # noqa: E712
    elif reviewed == "0":
        conditions.append(Transaction.is_reviewed == False)  # noqa: E712

    # ── Filtered base query ────────────────────────────────────────────────
    base_q = (
        db.query(Transaction)
        .outerjoin(Category, Transaction.category_id == Category.id)
        .filter(*conditions)
    )

    total = base_q.count()
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = min(page, total_pages)

    transactions = (
        base_q
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .offset((page - 1) * _PAGE_SIZE)
        .limit(_PAGE_SIZE)
        .all()
    )

    # ── Summary totals across the full filtered set (not just this page) ──
    total_debit = (
        db.query(func.sum(func.abs(Transaction.amount)))
        .filter(*conditions, Transaction.type == "debit")
        .scalar()
        or 0.0
    )
    total_credit = (
        db.query(func.sum(func.abs(Transaction.amount)))
        .filter(*conditions, Transaction.type == "credit")
        .scalar()
        or 0.0
    )

    # ── Dropdown data ──────────────────────────────────────────────────────
    categories = db.query(Category).order_by(Category.display_name).all()
    account_types = [
        row[0] for row in
        db.query(Transaction.account_type)
        .filter(Transaction.account_type != None, Transaction.user_id == current_user.id)  # noqa: E711
        .distinct()
        .order_by(Transaction.account_type)
        .all()
    ]

    # Build a query-string fragment (without page) for pagination links
    filter_qs = urlencode({k: v for k, v in {
        "q": q,
        "type": type,
        "category_id": category_id,
        "account": account,
        "from_date": from_date,
        "to_date": to_date,
        "is_transfer": is_transfer,
        "reviewed": reviewed,
    }.items() if v})

    return templates.TemplateResponse(request, "transactions.html", {
        "transactions": transactions,
        "categories": categories,
        "account_types": account_types,
        "total": total,
        "total_pages": total_pages,
        "page": page,
        "page_size": _PAGE_SIZE,
        "total_debit": round(total_debit, 2),
        "total_credit": round(total_credit, 2),
        "filter_qs": filter_qs,
        # Filter state (repopulate form)
        "f_q": q,
        "f_type": type,
        "f_category_id": category_id,
        "f_account": account,
        "f_from_date": from_date,
        "f_to_date": to_date,
        "f_is_transfer": is_transfer,
        "f_reviewed": reviewed,
        "current_user": current_user,
    })


@router.post("/review/confirm")
def confirm_transactions(
    request: Request,
    tx_ids: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ids = [int(i) for i in tx_ids.split(",") if i.strip()]
    db.query(Transaction).filter(
        Transaction.id.in_(ids), Transaction.user_id == current_user.id
    ).update({"is_reviewed": True}, synchronize_session=False)
    db.commit()
    return RedirectResponse(url="/review", status_code=303)


@router.post("/review/update-type")
def update_type(
    request: Request,
    tx_id: int = Form(...),
    type: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if type not in ("debit", "credit"):
        return JSONResponse({"ok": False}, status_code=400)
    tx = db.query(Transaction).filter(
        Transaction.id == tx_id, Transaction.user_id == current_user.id
    ).first()
    if tx:
        tx.type = type
        db.commit()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/review", status_code=303)


@router.post("/review/update-category")
def update_category(
    request: Request,
    tx_id: int = Form(...),
    category_id: str = Form(default=""),
    is_transfer: str = Form(default="0"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tx = db.query(Transaction).filter(
        Transaction.id == tx_id, Transaction.user_id == current_user.id
    ).first()
    if tx:
        resolved_cat_id = int(category_id) if category_id.strip() else None
        tx.category_id = resolved_cat_id
        tx.is_transfer = is_transfer == "1"
        db.commit()

        # Store as a learning example so future uploads benefit from this correction
        if resolved_cat_id is not None:
            existing = (
                db.query(CategorizationExample)
                .filter(
                    CategorizationExample.description == tx.description,
                    CategorizationExample.user_id == current_user.id,
                )
                .first()
            )
            if existing:
                existing.category_id = resolved_cat_id
                existing.created_at = datetime.now(timezone.utc)
            else:
                db.add(CategorizationExample(
                    user_id=current_user.id,
                    description=tx.description,
                    category_id=resolved_cat_id,
                ))
            db.commit()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/review", status_code=303)


@router.post("/review/add-transaction")
def add_transaction(
    request: Request,
    date: str = Form(...),
    description: str = Form(...),
    amount: str = Form(...),
    type: str = Form(...),
    account_type: str = Form(default="savings"),
    transaction_date: str = Form(default=""),
    category_id: str = Form(default=""),
    is_transfer: str = Form(default="0"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if type not in ("debit", "credit"):
        return RedirectResponse(url="/review", status_code=303)

    try:
        parsed_amount = float(amount)
        if parsed_amount <= 0:
            raise ValueError()
    except ValueError:
        return RedirectResponse(url="/review", status_code=303)

    raw_hash = f"{date}|{description.strip().lower()}|{parsed_amount:.2f}|manual"
    tx_hash = hashlib.sha256(raw_hash.encode()).hexdigest()

    existing = db.query(Transaction).filter(
        Transaction.hash == tx_hash, Transaction.user_id == current_user.id
    ).first()
    if existing:
        return RedirectResponse(url="/review", status_code=303)

    from datetime import date as date_type
    try:
        parsed_date = date_type.fromisoformat(date)
    except ValueError:
        return RedirectResponse(url="/review", status_code=303)

    resolved_cat_id = int(category_id) if category_id.strip() else None
    parsed_tx_date = None
    if transaction_date.strip():
        try:
            parsed_tx_date = date_type.fromisoformat(transaction_date.strip())
        except ValueError:
            pass

    tx = Transaction(
        user_id=current_user.id,
        statement_id=None,
        date=parsed_date,
        transaction_date=parsed_tx_date,
        description=description.strip(),
        amount=parsed_amount,
        type=type,
        account_type=account_type,
        is_transfer=is_transfer == "1",
        is_reviewed=False,
        category_id=resolved_cat_id,
        hash=tx_hash,
    )
    db.add(tx)
    db.commit()
    return RedirectResponse(url="/review?added=1", status_code=303)


@router.post("/transactions/{tx_id}/tag-reimbursement")
def tag_reimbursement(
    tx_id: int,
    category_id: str = Form(default=""),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tx = db.query(Transaction).filter(
        Transaction.id == tx_id, Transaction.user_id == current_user.id
    ).first()
    if not tx:
        return JSONResponse({"ok": False}, status_code=404)
    tx.reimbursement_category_id = int(category_id) if category_id.strip() else None
    db.commit()
    return JSONResponse({"ok": True})


@router.post("/transactions/{tx_id}/set-split")
def set_split(
    tx_id: int,
    category_id:       str = Form(default=""),
    split_start_month: str = Form(default=""),
    split_end_month:   str = Form(default=""),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tx = db.query(Transaction).filter(
        Transaction.id == tx_id, Transaction.user_id == current_user.id
    ).first()
    if not tx:
        return JSONResponse({"ok": False}, status_code=404)

    if split_start_month.strip() and split_end_month.strip():
        tx.reimbursement_category_id = int(category_id) if category_id.strip() else None
        tx.split_start_month = split_start_month.strip()
        tx.split_end_month   = split_end_month.strip()
    else:
        tx.split_start_month = None
        tx.split_end_month   = None
        tx.reimbursement_category_id = None

    db.commit()
    return JSONResponse({"ok": True})


@router.post("/review/delete-transaction")
def delete_transaction(
    request: Request,
    tx_id: int = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tx = db.query(Transaction).filter(
        Transaction.id == tx_id, Transaction.user_id == current_user.id
    ).first()
    if tx:
        db.delete(tx)
        db.commit()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/review", status_code=303)
