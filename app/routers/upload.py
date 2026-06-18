import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.limiter import limiter
from app.models import StatementUpload, Transaction, User
from app.services.pdf_parser import (
    compute_transaction_hash,
    extract_text_from_pdf,
    parse_date,
    parse_statement_with_gpt,
)

router = APIRouter(tags=["upload"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")

UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

SUPPORTED_BANKS = [
    "DBS / POSB",
    "DBS PayLah!",
    "Citibank",
    "Standard Chartered",
    "GXS",
    "MariBank",
    "Other",
]


def _error_page(request: Request, db: Session, error: str, status_code: int = 400, current_user=None):
    statements = db.query(StatementUpload).order_by(StatementUpload.upload_date.desc()).all()
    return templates.TemplateResponse(
        request, "upload.html",
        {"banks": SUPPORTED_BANKS, "statements": statements, "error": error, "current_user": current_user},
        status_code=status_code,
    )


@router.get("/upload")
def upload_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    statements = (
        db.query(StatementUpload)
        .filter(StatementUpload.user_id == current_user.id)
        .order_by(StatementUpload.upload_date.desc())
        .all()
    )
    skipped_map = {
        s.id: json.loads(s.skipped_json)
        for s in statements
        if s.skipped_json
    }

    # Aggregate debit/credit totals per statement in one query
    rows = (
        db.query(
            Transaction.statement_id,
            Transaction.type,
            func.sum(Transaction.amount).label("total"),
        )
        .filter(Transaction.statement_id.in_([s.id for s in statements]))
        .group_by(Transaction.statement_id, Transaction.type)
        .all()
    )
    totals_map: dict[int, dict] = {}
    for row in rows:
        totals_map.setdefault(row.statement_id, {})
        totals_map[row.statement_id][row.type] = row.total

    return templates.TemplateResponse(request, "upload.html", {
        "banks": SUPPORTED_BANKS,
        "statements": statements,
        "skipped_map": skipped_map,
        "totals_map": totals_map,
        "current_user": current_user,
    })


@router.post("/upload")
@limiter.limit("10/hour")
async def upload_statement(
    request: Request,
    file: UploadFile = File(...),
    bank_source: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # ── A1: Validate file extension ───────────────────────────────────────
    if not file.filename.lower().endswith(".pdf"):
        return _error_page(request, db, "Only PDF files are supported.")

    # ── A1: Validate PDF magic bytes (%PDF) ──────────────────────────────
    header = await file.read(4)
    await file.seek(0)
    if header != b"%PDF":
        return _error_page(request, db, "Only PDF files are supported.")

    # ── A1: Enforce 10 MB size limit ─────────────────────────────────────
    content = await file.read()
    await file.seek(0)
    if len(content) > MAX_UPLOAD_BYTES:
        return _error_page(request, db, "File too large. Maximum upload size is 10 MB.")

    # Save PDF to uploads/
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_name = f"{timestamp}_{file.filename}"
    save_path = UPLOAD_DIR / safe_name
    with open(save_path, "wb") as f:
        f.write(content)

    # Create statement record (status=processing)
    statement = StatementUpload(
        user_id=current_user.id,
        filename=file.filename,
        bank_source=bank_source,
        status="processing",
    )
    db.add(statement)
    db.commit()
    db.refresh(statement)

    try:
        # Step 1: Extract raw text from PDF
        raw_text = extract_text_from_pdf(str(save_path))
        statement.raw_text = raw_text

        # Step 2: Parse transactions (+ closing balance) with GPT
        parsed = parse_statement_with_gpt(raw_text, bank_source)
        statement.closing_balance = parsed.get("closing_balance")
        statement.account_type    = parsed.get("account_type")

        # Step 3: Insert transactions (skip duplicates by hash, scoped to user)
        user_id = current_user.id
        inserted = 0
        skipped = 0
        skipped_txs: list[dict] = []
        seen_hashes: set[str] = set()
        for t in parsed["transactions"]:
            try:
                tx_date = parse_date(str(t["date"]))
            except ValueError:
                continue  # Skip unparseable dates

            amount = float(t.get("amount", 0))
            if amount <= 0:
                continue  # Skip zero/negative amounts

            tx_hash = compute_transaction_hash(
                str(tx_date), t["description"], amount, bank_source,
                t.get("reference_id") or ""
            )

            existing = None if tx_hash not in seen_hashes else True
            if existing is None:
                existing = (
                    db.query(Transaction)
                    .filter(Transaction.hash == tx_hash, Transaction.user_id == user_id)
                    .first()
                )
            if existing:
                skipped_txs.append({
                    "date": str(tx_date),
                    "description": str(t["description"]).strip(),
                    "amount": amount,
                    "type": str(t.get("type", "debit")).lower(),
                })
                skipped += 1
                continue
            seen_hashes.add(tx_hash)

            raw_tx_date = t.get("transaction_date")
            tx_actual_date = None
            if raw_tx_date and str(raw_tx_date).strip():
                try:
                    tx_actual_date = parse_date(str(raw_tx_date).strip())
                except ValueError:
                    pass

            tx = Transaction(
                user_id=user_id,
                statement_id=statement.id,
                date=tx_date,
                transaction_date=tx_actual_date,
                description=str(t["description"]).strip(),
                amount=amount,
                type=str(t.get("type", "debit")).lower(),
                is_transfer=bool(t.get("is_transfer", False)),
                account_type=str(t.get("account_type", "other")) if t.get("account_type") else None,
                is_reviewed=False,
                hash=tx_hash,
            )
            db.add(tx)
            inserted += 1

        statement.status = "completed"
        statement.skipped_json = json.dumps(skipped_txs) if skipped_txs else None
        db.commit()

        # ── H: Delete PDF after successful parse ──────────────────────────
        try:
            save_path.unlink(missing_ok=True)
        except OSError:
            pass

        # Step 4: Auto-categorize new transactions with AI
        cat_failed = False
        if inserted > 0:
            from app.services.categorizer import batch_categorize_transactions
            cat_failed = not batch_categorize_transactions(statement.id, db)

        redirect_url = f"/review?new=1&inserted={inserted}&skipped={skipped}"
        if cat_failed:
            redirect_url += f"&cat_failed=1&statement_id={statement.id}"
        return RedirectResponse(url=redirect_url, status_code=303)

    except Exception as e:
        statement.status = "failed"
        db.commit()
        # ── A2: Sanitize error messages in production ─────────────────────
        if os.getenv("ENVIRONMENT", "development") == "production":
            error_msg = "An error occurred while processing the file. Please try again."
        else:
            error_msg = str(e)
        return _error_page(request, db, error_msg, status_code=500, current_user=current_user)


@router.post("/statements/{statement_id}/extract-balance")
def extract_statement_balance(
    statement_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Re-run GPT on stored raw_text to extract the closing balance and account_type.
    Safe to call on existing statements without re-uploading the PDF."""
    statement = db.query(StatementUpload).filter(
        StatementUpload.id == statement_id,
        StatementUpload.user_id == current_user.id,
    ).first()
    if statement and statement.raw_text:
        result = parse_statement_with_gpt(statement.raw_text, statement.bank_source)
        statement.closing_balance = result.get("closing_balance")
        statement.account_type    = result.get("account_type")
        db.commit()
    return RedirectResponse(url="/upload?balance_extracted=1", status_code=303)


@router.post("/statements/{statement_id}/recategorize")
def recategorize_statement(
    statement_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    statement = db.query(StatementUpload).filter(
        StatementUpload.id == statement_id,
        StatementUpload.user_id == current_user.id,
    ).first()
    if not statement:
        return RedirectResponse(url="/upload", status_code=303)
    from app.services.categorizer import batch_categorize_transactions
    ok = batch_categorize_transactions(statement_id, db)
    if ok:
        return RedirectResponse(url="/review", status_code=303)
    return RedirectResponse(
        url=f"/review?cat_failed=1&statement_id={statement_id}",
        status_code=303,
    )


@router.post("/statements/{statement_id}/delete")
def delete_statement(
    statement_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    statement = db.query(StatementUpload).filter(
        StatementUpload.id == statement_id,
        StatementUpload.user_id == current_user.id,
    ).first()
    if statement:
        db.query(Transaction).filter(Transaction.statement_id == statement_id).delete()
        db.delete(statement)
        db.commit()
        # PDFs are deleted right after parse; this handles any orphans from old uploads
        for f in UPLOAD_DIR.glob(f"*_{statement.filename}"):
            try:
                f.unlink()
            except OSError:
                pass
    return RedirectResponse(url="/upload?deleted=1", status_code=303)
