"""Inspect the three-stage extraction pipeline output.

Usage:
    python debug_csv.py path/to/statement.pdf [bank_source]

bank_source defaults to "DBS / POSB" if omitted.
Saves a _debug.csv alongside the PDF if extraction succeeds.
"""
import sys
import os
import json
import base64

sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()

import pymupdf as fitz
from app.services.pdf_parser import (
    _get_page_text_lines,
    _detect_layout,
    _find_column_boundaries,
    _extract_rows_from_page,
    _rows_to_csv,
    pdf_to_base64_images,
)

if len(sys.argv) < 2:
    print("Usage: python debug_csv.py path/to/statement.pdf [bank_source] [--verbose]")
    sys.exit(1)

pdf_path = sys.argv[1]
bank_source = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else "DBS / POSB"
verbose = "--verbose" in sys.argv

print(f"=== {pdf_path} | bank: {bank_source} ===\n")

# ── Step 1: render and extract text lines ─────────────────────────────────────
doc = fitz.open(pdf_path)
mat = fitz.Matrix(150 / 72, 150 / 72)
images = []
all_text_lines = []
for page_num, page in enumerate(doc, start=1):
    pix = page.get_pixmap(matrix=mat)
    images.append(base64.b64encode(pix.tobytes("png")).decode())
    lines = _get_page_text_lines(page, page_num)
    all_text_lines.append(lines)
    print(f"Page {page_num}: {len(lines)} native text lines")
doc.close()

# ── Step 2: Stage 1 Vision layout detection ───────────────────────────────────
print("\n--- Calling GPT-4o for layout detection (Stage 1)... ---\n")
layout = _detect_layout(images, all_text_lines)
print("Layout result:")
print(json.dumps(layout, indent=2))

# ── Step 3: Stage 2 PyMuPDF extraction ───────────────────────────────────────
print("\n--- Running PyMuPDF extraction (Stage 2) ---\n")
doc2 = fitz.open(pdf_path)
all_rows = []
field_names = []

for page_info in layout.get("pages", []):
    if not page_info.get("has_transaction_table"):
        print(f"Page {page_info['page_number']}: no transaction table — skipping")
        continue

    page_num = page_info["page_number"]
    columns = page_info.get("columns", [])
    page = doc2[page_num - 1]
    text_lines = all_text_lines[page_num - 1]
    page_width = page.rect.width

    col_defs = _find_column_boundaries(columns, text_lines, page_width)
    print(f"Page {page_num}: {len(col_defs)} columns located:")
    for c in col_defs:
        print(f"  {c['field']:12s}  '{c['header_text']}'  x=[{c['x_start']:.1f}, {c['x_end']:.1f}]  header_y_bottom={c['header_y_bottom']:.1f}")

    _PDF_SCALE = 72.0 / 150.0
    bbox = page_info.get("table_bbox")
    table_y_end = bbox[3] * _PDF_SCALE if bbox else None
    if table_y_end:
        print(f"  table_bbox={bbox}  -> table_y_end (PDF pts)={table_y_end:.1f}")

    rows = _extract_rows_from_page(page, col_defs, table_y_end=table_y_end)
    print(f"  → {len(rows)} rows extracted")
    if verbose:
        from app.services.pdf_parser import _get_page_text_lines as _gtl
        import pymupdf as _fitz
        _doc_v = _fitz.open(pdf_path)
        _pg_v = _doc_v[page_num - 1]
        _words = _pg_v.get_text("words", sort=True)
        _table_y = max(c["header_y_bottom"] for c in col_defs)
        _below = [w for w in _words if w[1] > _table_y]
        _vrows: list[dict] = []
        for _w in _below:
            _yc = (_w[1] + _w[3]) / 2
            _xc = (_w[0] + _w[2]) / 2
            if _vrows and abs(_yc - _vrows[-1]["y"]) <= 8.0:
                _vrows[-1]["words"].append((_xc, _w[4]))
            else:
                _vrows.append({"y": _yc, "words": [(_xc, _w[4])]})
        _doc_v.close()
        print(f"  [verbose] {len(_vrows)} raw visual rows on page {page_num}:")
        for _i, _vr in enumerate(_vrows):
            print(f"    row {_i:3d} y={_vr['y']:6.1f}: {_vr['words']}")
    all_rows.extend(rows)
    if not field_names and col_defs:
        field_names = [c["field"] for c in col_defs]

doc2.close()

# ── Step 4: Save CSV ──────────────────────────────────────────────────────────
if all_rows and field_names:
    table_csv = _rows_to_csv(all_rows, field_names)
    out_path = pdf_path.replace(".pdf", "_debug.csv")
    with open(out_path, "w", encoding="utf-8-sig") as f:
        f.write(table_csv)
    lines = table_csv.splitlines()
    print(f"\nCSV saved to: {out_path}  ({len(lines) - 1} rows)")
    print(f"\n--- First 30 rows ---\n")
    for line in lines[:31]:
        print(line)
    if len(lines) > 31:
        print(f"... ({len(lines) - 1} rows total)")
else:
    print("\nNo rows extracted — would fall back to Vision.")
