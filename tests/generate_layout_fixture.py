"""Generate the Stage 1 layout fixture for may26_dbs.pdf.

Run once (requires OPENAI_API_KEY and may26_dbs.pdf in the project root):

    venv\\Scripts\\python tests/generate_layout_fixture.py

Saves the gpt-4o Vision response to tests/fixtures/may26_dbs_layout.json so that
the regression tests in test_pdf_pipeline.py can run without any API calls.
Re-run this script only if the Stage 1 prompt or PDF changes.
"""

import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PDF_PATH = ROOT / "may26_dbs.pdf"
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "may26_dbs_layout.json"


def main() -> None:
    if not PDF_PATH.exists():
        print(f"ERROR: {PDF_PATH} not found. Place may26_dbs.pdf in the project root.")
        sys.exit(1)

    import fitz
    from app.services.pdf_parser import _detect_layout, _get_page_text_lines

    print(f"Rendering {PDF_PATH.name} ...")
    doc = fitz.open(str(PDF_PATH))
    mat = fitz.Matrix(150 / 72, 150 / 72)
    images: list[str] = []
    all_text_lines: list[list[dict]] = []
    for page_num, page in enumerate(doc, start=1):
        pix = page.get_pixmap(matrix=mat)
        images.append(base64.b64encode(pix.tobytes("png")).decode())
        all_text_lines.append(_get_page_text_lines(page, page_num))
    doc.close()

    print(f"Calling gpt-4o Vision for layout detection ({len(images)} pages) ...")
    layout = _detect_layout(images, all_text_lines)

    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(json.dumps(layout, indent=2), encoding="utf-8")
    print(f"Fixture saved to {FIXTURE_PATH}")

    tabular = [p for p in layout["pages"] if p.get("has_transaction_table")]
    print(f"  {len(tabular)} tabular pages out of {len(layout['pages'])} total")
    for p in tabular:
        cols = [c["field"] for c in p.get("columns", [])]
        print(f"  Page {p['page_number']}: {cols}  bbox={p.get('table_bbox')}")


if __name__ == "__main__":
    main()
