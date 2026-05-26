"""
Phase 3 M8a gate — word/token geometry capture (preprocess/word_geometry.py).

Fully offline — a synthetic DocAI-Document-like object (SimpleNamespace) stands in
for the real proto. Checks:
  [1] extract_word_geometry pulls token boxes + char offsets + page from the OCR layer.
  [2] no-token document → extract returns [] (Layout-Parser-only → UI falls back).
  [3] geometric containment (_center_inside).
  [4] word_boxes_for_entity returns the tight per-line box for a matching surface,
      and falls back to the block box when the surface isn't found / no tokens.

Run:  PYTHONPATH=. python scripts/gates/gate_p3_m8a_token_geometry.py
"""

from __future__ import annotations

import sys
from types import SimpleNamespace as NS

from preprocess.word_geometry import (
    _center_inside,
    extract_word_geometry,
    interpolate_entity_box,
    word_boxes_for_entity,
)

# doc.text = "HER2 negative EGFR amplified"
#             0   4 5      13 14 18 19      28
_TEXT = "HER2 negative EGFR amplified"


def _tok(x0, y0, x1, y1, start, end):
    poly = NS(normalized_vertices=[NS(x=x0, y=y0), NS(x=x1, y=y0), NS(x=x1, y=y1), NS(x=x0, y=y1)])
    anchor = NS(text_segments=[NS(start_index=start, end_index=end)])
    return NS(layout=NS(bounding_poly=poly, text_anchor=anchor))


def _document():
    tokens = [
        _tok(0.10, 0.20, 0.18, 0.23, 0, 4),     # HER2
        _tok(0.19, 0.20, 0.30, 0.23, 5, 13),    # negative
        _tok(0.10, 0.30, 0.18, 0.33, 14, 18),   # EGFR
        _tok(0.19, 0.30, 0.32, 0.33, 19, 28),   # amplified
    ]
    return NS(text=_TEXT, pages=[NS(page_number=1, tokens=tokens)])


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    print("[1] extract token geometry")
    words = extract_word_geometry(_document())
    check("4 word boxes extracted", len(words) == 4, str(len(words)))
    by_text = {w["text"]: w for w in words}
    check("token text sliced from offsets", set(by_text) == {"HER2", "negative", "EGFR", "amplified"},
          str(sorted(by_text)))
    check("HER2 offsets + page", by_text.get("HER2", {}).get("char_start") == 0
          and by_text["HER2"]["char_end"] == 4 and by_text["HER2"]["page"] == 1)
    check("bbox is [x0,y0,x1,y1]", by_text["HER2"]["bbox"] == [0.10, 0.20, 0.18, 0.23],
          str(by_text["HER2"]["bbox"]))

    print("[2] no token layer → empty")
    check("layout-only doc → []", extract_word_geometry(NS(text="x", pages=[NS(page_number=1, tokens=[])])) == [])
    check("no pages → []", extract_word_geometry(NS(text="x", pages=[])) == [])

    print("[3] geometric containment")
    blk = [0.08, 0.18, 0.34, 0.25]   # covers the HER2/negative line, not EGFR/amplified
    check("HER2 center inside block", _center_inside([0.10, 0.20, 0.18, 0.23], blk) is True)
    check("EGFR center outside block", _center_inside([0.10, 0.30, 0.18, 0.33], blk) is False)

    print("[4] entity highlight mapper")
    boxes = word_boxes_for_entity(words, page=1, block_bbox=blk, surface="HER2 negative")
    check("matched run merged to one line box", len(boxes) == 1, str(boxes))
    check("merged box spans HER2..negative",
          boxes and abs(boxes[0][0] - 0.10) < 1e-6 and abs(boxes[0][2] - 0.30) < 1e-6, str(boxes))
    one = word_boxes_for_entity(words, page=1, block_bbox=blk, surface="HER2")
    check("single-word surface → that word's box", one == [[0.10, 0.20, 0.18, 0.23]], str(one))
    missing = word_boxes_for_entity(words, page=1, block_bbox=blk, surface="KRAS")
    check("surface not found → fallback to block box", missing == [blk], str(missing))
    no_tokens = word_boxes_for_entity([], page=1, block_bbox=blk, surface="HER2")
    check("no tokens → fallback to block box", no_tokens == [blk], str(no_tokens))
    check("EGFR not pulled into HER2's block", "amplified" not in str(boxes))

    print("[5] no-OCR character interpolation (the fallback for token-less docs)")
    # single-line block: "HER2 IHC equivocal" (18 chars), surface "equivocal" = right half
    blk2 = [0.10, 0.20, 0.90, 0.25]
    boxes = interpolate_entity_box(blk2, "HER2 IHC equivocal", "equivocal")
    check("single-line interpolation returns one box", len(boxes) == 1, str(boxes))
    check("box is a right-half x-slice of the block",
          boxes and abs(boxes[0][0] - 0.50) < 0.02 and abs(boxes[0][2] - 0.90) < 0.02
          and boxes[0][1] == 0.20 and abs(boxes[0][3] - 0.25) < 1e-9, str(boxes))
    check("surface not in block text → []", interpolate_entity_box(blk2, "HER2 IHC equivocal", "KRAS") == [])
    # word_boxes_for_entity with NO tokens but block_text present → interpolates (not block box)
    interp = word_boxes_for_entity([], page=1, block_bbox=blk2, surface="equivocal",
                                   block_text="HER2 IHC equivocal")
    check("word_boxes_for_entity falls back to interpolation (tighter than block)",
          len(interp) == 1 and interp[0] != blk2, str(interp))
    check("no surface match → block box fallback",
          word_boxes_for_entity([], page=1, block_bbox=blk2, surface="ZZZ",
                                block_text="HER2 IHC equivocal") == [blk2])

    print("-" * 60)
    if fails:
        print(f"P3-M8a VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("P3-M8a VERIFY: PASS — token geometry captured; entity highlight mapper + fallback work.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
