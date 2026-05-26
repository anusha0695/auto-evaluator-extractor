"""
Phase 2 M2.5 verification gate — DocAI table structure + cross-page stitching.

Done-when (PHASE_2_PLAN.md M2.5):
  - results-table cells carry (table_id, row, col, row_span, col_span);
  - a spanning header cell's col_span is captured (a wide row is reconstructable);
  - a table split across a page break stitches into one table_id, with the
    page-2 rows renumbered after the page-1 rows and table_continued=True.

The real DocAI parse needs a cloud call, but the walk + stitch logic is pure, so
we drive it with a synthetic DocAI-Layout proto stand-in (duck-typed objects
matching the attributes preprocess/docai_parser.py reads).

Run:  PYTHONPATH=. python scripts/gates/gate_p2_m25_table_coords_stitch.py
"""

from __future__ import annotations

import sys
from types import SimpleNamespace as NS

from preprocess.docai_parser import DocAIParser


def vtx(x, y):
    return NS(x=x, y=y)


def leaf(block_id, page, text):
    """A leaf text block (what lives inside a table cell)."""
    return NS(
        block_id=block_id,
        page_span=NS(page_start=page),
        text_block=NS(text=text, blocks=None, type_="text"),
        table_block=None, list_block=None, image_block=None,
        bounding_box=NS(normalized_vertices=[vtx(0.1, 0.1), vtx(0.2, 0.2)], vertices=[]),
    )


def cell(block_id, page, text, *, row_span=1, col_span=1):
    return NS(blocks=[leaf(block_id, page, text)], row_span=row_span, col_span=col_span)


def row(cells):
    return NS(cells=cells)


def table(block_id, page, *, header_rows, body_rows):
    return NS(
        block_id=block_id,
        page_span=NS(page_start=page),
        text_block=None, list_block=None, image_block=None,
        bounding_box=NS(normalized_vertices=[vtx(0.0, 0.0), vtx(1.0, 0.5)], vertices=[]),
        table_block=NS(header_rows=header_rows, body_rows=body_rows),
    )


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    # --- Page 1: a table with a SPANNING title header (col_span=2), a column-
    #     header row, and two body rows. Page 2: a HEADERLESS continuation with
    #     the same 2-column shape. ---
    t1 = table(
        "t1", 1,
        header_rows=[
            row([cell("c-title", 1, "JAK2 V617F Mutation Analysis", col_span=2)]),
            row([cell("c-h0", 1, "Gene"), cell("c-h1", 1, "Result")]),
        ],
        body_rows=[
            row([cell("c-a0", 1, "JAK2"), cell("c-a1", 1, "Detected")]),
            row([cell("c-b0", 1, "BRAF"), cell("c-b1", 1, "Negative")]),
        ],
    )
    t2 = table(
        "t2", 2,
        header_rows=[],  # continuation — no header
        body_rows=[
            row([cell("c-c0", 2, "EGFR"), cell("c-c1", 2, "Positive")]),
        ],
    )
    document = NS(document_layout=NS(blocks=[t1, t2]), pages=[], text="")

    parser = object.__new__(DocAIParser)  # bypass __init__ (no cloud creds needed)
    profile = DocAIParser._document_to_doc_profile(parser, document, raw_uri="local://test")
    blocks = profile["blocks"]
    by_text = {b["text"]: b for b in blocks}

    print("[1] table-structure coordinates on cells")
    cells = [b for b in blocks if b.get("table_id")]
    containers = [b for b in blocks if not b.get("table_id")]
    # 9 cells (1 title + 2 col-headers + 4 body + 2 continuation) + 2 table
    # container blocks (whole-table bbox, no table_id — used for the UI overlay).
    check("9 table cells carry table_id", len(cells) == 9, f"{len(cells)} cells / {len(blocks)} blocks")
    check("2 table-container blocks (no table_id)", len(containers) == 2, str(len(containers)))
    g = by_text.get("Gene", {})
    check("'Gene' is header row1 col0", g.get("is_table_header") and g.get("row") == 1 and g.get("col") == 0,
          f"row={g.get('row')} col={g.get('col')} hdr={g.get('is_table_header')}")
    jak = by_text.get("JAK2", {})
    check("'JAK2' body row2 col0, table_id t1", jak.get("row") == 2 and jak.get("col") == 0 and jak.get("table_id") == "t1",
          f"row={jak.get('row')} col={jak.get('col')} tid={jak.get('table_id')}")

    print("[2] spanning header captured")
    title = by_text.get("JAK2 V617F Mutation Analysis", {})
    check("title cell col_span=2, header, row0", title.get("col_span") == 2 and title.get("is_table_header") and title.get("row") == 0,
          f"col_span={title.get('col_span')} row={title.get('row')}")

    print("[3] cross-page stitching")
    egfr = by_text.get("EGFR", {})
    check("EGFR merged into t1", egfr.get("table_id") == "t1", f"tid={egfr.get('table_id')}")
    check("EGFR row renumbered after page-1 rows", egfr.get("row") == 4, f"row={egfr.get('row')} (expected 4 = max_row 3 + 1)")
    check("EGFR flagged table_continued", egfr.get("table_continued") is True)
    check("page-1 header still row<EGFR (bindable)", g.get("row") < egfr.get("row"))

    print("-" * 60)
    if fails:
        print(f"M2.5 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("M2.5 VERIFY: PASS — table coords captured + continuation stitched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
