import json
import logging
import os
import secrets
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Must exceed the parser's 600s request timeout plus categorisation, or this lazy
# expiry marks a still-running parse as failed. High-effort reasoning over a full
# statement runs for minutes.
_PROCESSING_TIMEOUT = timedelta(minutes=15)

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.dependencies import get_current_user
from app.limiter import limiter
from app.models import StatementUpload, Transaction, User

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
    try:
        query = db.query(StatementUpload)
        if current_user:
            query = query.filter(StatementUpload.user_id == current_user.id)
        statements = query.order_by(StatementUpload.upload_date.desc()).all()
    except Exception:
        statements = []
    return templates.TemplateResponse(
        request, "upload.html",
        {
            "banks": SUPPORTED_BANKS,
            "statements": statements,
            "error": error,
            "current_user": current_user,
            "skipped_map": {},
            "totals_map": {},
        },
        status_code=status_code,
    )


def _mark_failed(statement_id: int, statement, db) -> None:
    """Mark a statement as failed.

    Tries the existing session first; if that session is in a broken state
    (e.g. mid-exception rollback), opens a fresh connection as fallback so
    the status update always lands even if the primary session is unusable.
    """
    if statement is not None:
        try:
            statement.status = "failed"
            db.commit()
            return
        except Exception:
            pass
    try:
        fresh = SessionLocal()
        s = fresh.query(StatementUpload).filter_by(id=statement_id).first()
        if s:
            s.status = "failed"
            fresh.commit()
        fresh.close()
    except Exception:
        logger.error("Could not mark statement %d as failed via any session", statement_id)


def _process_statement_background(
    statement_id: int, save_path: str, bank_source: str, user_id: int
) -> None:
    """Parse the PDF and insert transactions. Runs in a background thread after the
    HTTP response is already sent, so the 30-second Render proxy timeout never fires.

    Structured in two independent phases:
    - Phase A: parse + insert + commit("completed"). On failure: mark "failed" and return.
    - Phase B: categorize. Runs only after Phase A succeeds. Failure here is non-fatal
      and never touches statement.status — transactions are already committed.
    """
    from app.services.pdf_parser import (
        compute_transaction_hash,
        parse_date,
        parse_statement,
    )

    db = SessionLocal()
    statement = None
    inserted = 0  # must be declared before Phase A so Phase B can read it

    # ── Phase A: parse PDF + insert transactions ──────────────────────────────
    try:
        statement = db.query(StatementUpload).filter_by(id=statement_id).first()
        if not statement:
            db.close()
            return

        parsed = parse_statement(save_path, bank_source)
        statement.closing_balance = parsed.get("closing_balance")
        statement.account_type = parsed.get("account_type")

        skipped_txs: list[dict] = []
        seen_hashes: set[str] = set()

        for t in parsed["transactions"]:
            try:
                tx_date = parse_date(str(t["date"]))
            except ValueError:
                continue
            amount = float(t.get("amount", 0))
            if amount <= 0:
                continue

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
                    "is_transfer": bool(t.get("is_transfer", False)),
                    "account_type": str(t.get("account_type", "other")) if t.get("account_type") else None,
                    "hash": tx_hash,
                })
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
                statement_id=statement_id,
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
        db.commit()  # "completed" is permanent after this line

    except Exception:
        logger.error("Background parse failed for statement %d:\n%s",
                     statement_id, traceback.format_exc())
        _mark_failed(statement_id, statement, db)
        try:
            Path(save_path).unlink(missing_ok=True)
        except OSError:
            pass
        db.close()
        return  # Phase B must NOT run if Phase A failed

    # Clean up PDF on success path
    try:
        Path(save_path).unlink(missing_ok=True)
    except OSError:
        pass

    # ── Phase B: categorize (independent — never touches statement.status) ────
    if inserted > 0:
        try:
            from app.services.categorizer import batch_categorize_transactions
            batch_categorize_transactions(statement_id, db)
        except Exception:
            logger.error(
                "Categorizer failed for statement %d (transactions are committed OK):\n%s",
                statement_id, traceback.format_exc(),
            )

    db.close()


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

    processing_ids = [s.id for s in statements if s.status == "processing"]

    return templates.TemplateResponse(request, "upload.html", {
        "banks": SUPPORTED_BANKS,
        "statements": statements,
        "skipped_map": skipped_map,
        "totals_map": totals_map,
        "current_user": current_user,
        "processing_ids": processing_ids,
    })


@router.get("/upload/status/{statement_id}")
def get_statement_status(
    statement_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    statement = db.query(StatementUpload).filter(
        StatementUpload.id == statement_id,
        StatementUpload.user_id == current_user.id,
    ).first()
    if not statement:
        return JSONResponse({"status": "not_found"}, status_code=404)

    # Auto-expire stuck processing statements so the UI doesn't wait forever.
    if statement.status == "processing":
        upload_dt = statement.upload_date
        if upload_dt.tzinfo is None:
            upload_dt = upload_dt.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - upload_dt > _PROCESSING_TIMEOUT:
            statement.status = "failed"
            try:
                db.commit()
            except Exception:
                pass

    inserted = db.query(func.count(Transaction.id)).filter(
        Transaction.statement_id == statement_id,
        Transaction.user_id == current_user.id,
    ).scalar() or 0
    skipped = len(json.loads(statement.skipped_json or "[]"))

    upload_dt = statement.upload_date
    if upload_dt.tzinfo is None:
        upload_dt = upload_dt.replace(tzinfo=timezone.utc)

    return JSONResponse({
        "status": statement.status,
        "inserted": inserted,
        "skipped": skipped,
        "started_at": upload_dt.isoformat(),
    })


@router.post("/upload")
@limiter.limit("10/hour")
async def upload_statement(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    bank_source: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # ── Validate file extension ───────────────────────────────────────────────
    if not file.filename.lower().endswith(".pdf"):
        return _error_page(request, db, "Only PDF files are supported.", current_user=current_user)

    # ── Validate PDF magic bytes (%PDF) ──────────────────────────────────────
    header = await file.read(4)
    await file.seek(0)
    if header != b"%PDF":
        return _error_page(request, db, "Only PDF files are supported.", current_user=current_user)

    # ── Enforce 10 MB size limit ──────────────────────────────────────────────
    content = await file.read()
    await file.seek(0)
    if len(content) > MAX_UPLOAD_BYTES:
        return _error_page(request, db, "File too large. Maximum upload size is 10 MB.", current_user=current_user)

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

    # Schedule heavy work in a background thread — response returns immediately so
    # Render's 30-second proxy timeout never fires.
    background_tasks.add_task(
        _process_statement_background,
        statement_id=statement.id,
        save_path=str(save_path),
        bank_source=bank_source,
        user_id=current_user.id,
    )

    return RedirectResponse(url=f"/upload?processing={statement.id}", status_code=303)


@router.post("/statements/{statement_id}/restore-skipped")
def restore_skipped_transaction(
    statement_id: int,
    tx_hash: str = Form(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Force-insert a previously skipped (false-positive dedup) transaction."""
    from app.services.pdf_parser import parse_date

    statement = db.query(StatementUpload).filter(
        StatementUpload.id == statement_id,
        StatementUpload.user_id == current_user.id,
    ).first()
    if not statement or not statement.skipped_json:
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)

    skipped = json.loads(statement.skipped_json)
    entry = next((t for t in skipped if t.get("hash") == tx_hash), None)
    if entry is None:
        return JSONResponse({"ok": False, "error": "Transaction not found in skipped list"}, status_code=404)

    try:
        tx_date = parse_date(entry["date"])
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid date"}, status_code=400)

    tx = Transaction(
        user_id=current_user.id,
        statement_id=statement_id,
        date=tx_date,
        description=entry["description"],
        amount=float(entry["amount"]),
        type=entry.get("type", "debit"),
        is_transfer=entry.get("is_transfer", False),
        account_type=entry.get("account_type"),
        is_reviewed=False,
        hash=secrets.token_hex(32),  # unique hash; bypasses dedup intentionally
    )
    db.add(tx)

    skipped = [t for t in skipped if t.get("hash") != tx_hash]
    statement.skipped_json = json.dumps(skipped) if skipped else None
    db.commit()

    return JSONResponse({"ok": True, "remaining": len(skipped)})


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
        for f in UPLOAD_DIR.glob(f"*_{statement.filename}"):
            try:
                f.unlink()
            except OSError:
                pass
    return RedirectResponse(url="/upload?deleted=1", status_code=303)
