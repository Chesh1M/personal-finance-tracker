import json
import os

from openai import OpenAI
from sqlalchemy.orm import Session

from app.models import CategorizationExample, Category, Transaction


def batch_categorize_transactions(statement_id: int, db: Session) -> bool:
    """Auto-assign categories to all uncategorized transactions from a statement.

    Returns True on success (or when there's nothing to do), False if the GPT call fails.
    Transfers are assigned rule-based (no GPT call). Non-fatal: callers should handle False gracefully.
    """
    transactions = (
        db.query(Transaction)
        .filter(Transaction.statement_id == statement_id, Transaction.category_id.is_(None))
        .all()
    )
    if not transactions:
        return True

    categories = db.query(Category).order_by(Category.name).all()
    cat_by_name = {c.name: c for c in categories}
    transfer_cat = cat_by_name.get("transfers")

    # Rule-based: is_transfer=True → "transfers" category (no GPT needed)
    to_categorize = []
    for tx in transactions:
        if tx.is_transfer and transfer_cat:
            tx.category_id = transfer_cat.id
        else:
            to_categorize.append(tx)
    db.commit()

    if not to_categorize:
        return True

    spending_cats = [
        {"name": c.name, "label": c.display_name}
        for c in categories
        if not c.is_transfer
    ]
    tx_list = [
        {"i": i, "desc": tx.description, "amount": float(tx.amount), "type": tx.type}
        for i, tx in enumerate(to_categorize)
    ]

    # Inject past user corrections as few-shot examples (most recent 30, unique per description)
    examples = (
        db.query(CategorizationExample)
        .order_by(CategorizationExample.created_at.desc())
        .limit(30)
        .all()
    )
    cat_by_id = {c.id: c for c in categories}
    example_lines = [
        f'- "{ex.description}" -> {cat_by_id[ex.category_id].name}'
        for ex in examples
        if ex.category_id in cat_by_id
    ]
    examples_section = (
        "Previous corrections by this user (treat as ground truth):\n"
        + "\n".join(example_lines)
        + "\n\n"
        if example_lines else ""
    )

    prompt = (
        f"{examples_section}"
        f"Categorize each transaction using one of these categories:\n"
        f"{json.dumps(spending_cats)}\n\n"
        f"Transactions:\n{json.dumps(tx_list)}\n\n"
        f'Return JSON: {{"assignments": [{{"i": <index>, "category": <name>}}]}}\n'
        f'Use "others" when unsure. Singapore spending context.'
    )

    try:
        client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "You are a personal finance transaction categorizer for a user in Singapore.",
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        result = json.loads(resp.choices[0].message.content)
        for assignment in result.get("assignments", []):
            idx = assignment.get("i")
            cat_name = assignment.get("category")
            if idx is not None and 0 <= idx < len(to_categorize) and cat_name in cat_by_name:
                to_categorize[idx].category_id = cat_by_name[cat_name].id
        db.commit()
        return True
    except Exception:
        return False