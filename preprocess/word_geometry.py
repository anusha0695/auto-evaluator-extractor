"""
Word-level geometry (P3-M8a) — pixel-tight highlight support for the SME UI.

The Layout Parser locates BLOCKS (table cells, paragraphs); the finest geometry
DocAI exposes is the OCR **token** layer (`document.pages[].tokens[]`), each with a
bounding poly + a text-anchor char span into `document.text`. `extract_word_geometry`
captures those as flat `WordBox` records. When a document has no token layer (pure
Layout-Parser output), the list is empty and the UI falls back to the block box.

`word_boxes_for_entity` is the UI-facing mapper: given the selected entity's
containing-block bbox + its surface text, it returns the tight highlight box(es) —
the word boxes geometrically inside the block whose joined text matches the surface,
merged per line. If nothing matches (or no tokens), it returns the block box, so the
highlight degrades gracefully instead of disappearing.

All pure / offline — no DocAI client, no network. The gate drives it with a
synthetic document object.
"""

from __future__ import annotations

import re
from typing import Any


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s or "")).lower()


def _poly_to_bbox(poly: Any) -> list[float]:
    """[x0,y0,x1,y1] from a bounding poly's normalized (or absolute) vertices."""
    if not poly:
        return []
    verts = list(getattr(poly, "normalized_vertices", []) or []) \
        or list(getattr(poly, "vertices", []) or [])
    if not verts:
        return []
    xs = [float(getattr(v, "x", 0) or 0) for v in verts]
    ys = [float(getattr(v, "y", 0) or 0) for v in verts]
    return [min(xs), min(ys), max(xs), max(ys)]


def extract_word_geometry(document: Any) -> list[dict[str, Any]]:
    """Walk `document.pages[].tokens[]` → flat WordBox dicts
    {page, text, bbox:[x0,y0,x1,y1], char_start, char_end}. Returns [] when the
    response has no token layer (Layout-Parser-only output)."""
    full_text = getattr(document, "text", "") or ""
    out: list[dict[str, Any]] = []
    for page in (getattr(document, "pages", []) or []):
        pnum = int(getattr(page, "page_number", 0) or 0) or (len(out) and out[-1]["page"]) or 1
        for tok in (getattr(page, "tokens", []) or []):
            layout = getattr(tok, "layout", None)
            if layout is None:
                continue
            bbox = _poly_to_bbox(getattr(layout, "bounding_poly", None))
            if not bbox:
                continue
            cs = ce = None
            anchor = getattr(layout, "text_anchor", None)
            segs = list(getattr(anchor, "text_segments", []) or []) if anchor else []
            if segs:
                cs = int(getattr(segs[0], "start_index", 0) or 0)
                ce = int(getattr(segs[-1], "end_index", 0) or 0)
            text = full_text[cs:ce] if (cs is not None and ce is not None and full_text) else ""
            out.append({"page": int(pnum), "text": text, "bbox": bbox,
                        "char_start": cs, "char_end": ce})
    return out


def _center_inside(word_bbox: list[float], block_bbox: list[float], tol: float = 0.004) -> bool:
    """True iff the word box's center sits within the block box (small tolerance
    absorbs rounding at the edges)."""
    if not word_bbox or not block_bbox:
        return False
    cx = (word_bbox[0] + word_bbox[2]) / 2.0
    cy = (word_bbox[1] + word_bbox[3]) / 2.0
    return (block_bbox[0] - tol <= cx <= block_bbox[2] + tol
            and block_bbox[1] - tol <= cy <= block_bbox[3] + tol)


def _reading_order(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort words top-to-bottom, then left-to-right, bucketing by line (y-center)."""
    def key(w):
        b = w["bbox"]
        return (round((b[1] + b[3]) / 2.0, 2), b[0])
    return sorted(words, key=key)


def _merge_per_line(words: list[dict[str, Any]]) -> list[list[float]]:
    """Merge a run of word boxes into one box per line (grouped by y-center)."""
    lines: dict[float, list[list[float]]] = {}
    for w in words:
        b = w["bbox"]
        ykey = round((b[1] + b[3]) / 2.0, 2)
        lines.setdefault(ykey, []).append(b)
    out: list[list[float]] = []
    for _, boxes in sorted(lines.items()):
        xs0 = [b[0] for b in boxes]; ys0 = [b[1] for b in boxes]
        xs1 = [b[2] for b in boxes]; ys1 = [b[3] for b in boxes]
        out.append([min(xs0), min(ys0), max(xs1), max(ys1)])
    return out


def interpolate_entity_box(
    block_bbox: list[float], block_text: str, surface: str,
) -> list[list[float]]:
    """No-OCR sub-block geometry: estimate the rectangle(s) for `surface` inside a
    block from its character position in the block text. Accurate for single-line
    blocks (an x-slice); approximate for multi-line (estimates the line). Needs no
    tokens — derived purely from the block bbox + text we already have."""
    import math
    if not block_bbox or not block_text or not surface:
        return []
    s = surface.strip()
    idx = block_text.lower().find(s.lower())
    if idx < 0:
        return []
    cs, ce = idx, idx + len(s)
    x0, y0, x1, y1 = block_bbox
    w, h = x1 - x0, y1 - y0
    n = max(len(block_text), 1)
    # estimate line count from block aspect + text length (char aspect ~0.5 w/h)
    num_lines = 1 if (w <= 0 or h <= 0) else max(1, round(math.sqrt(n * h / (2.0 * w))))
    cpl = max(1, math.ceil(n / num_lines))           # chars per line
    line_h = h / num_lines
    ls, le = cs // cpl, (ce - 1) // cpl
    if ls == le:                                     # entity on one line (the common case)
        c0, c1 = cs - ls * cpl, ce - ls * cpl
        bx0 = x0 + (c0 / cpl) * w
        bx1 = x0 + (min(c1, cpl) / cpl) * w
        by0 = y0 + ls * line_h
        return [[bx0, by0, bx1, by0 + line_h]]
    return [[x0, y0 + ln * line_h, x1, y0 + (ln + 1) * line_h] for ln in range(ls, le + 1)]


def word_boxes_for_entity(
    words: list[dict[str, Any]],
    *,
    page: int,
    block_bbox: list[float],
    surface: str,
    block_text: str = "",
) -> list[list[float]]:
    """The UI highlight mapper. Returns the tight box(es) for `surface`:
      1. from OCR word tokens if present (pixel-exact),
      2. else interpolated from the block text (no-OCR, char-position based),
      3. else [block_bbox] as the last-resort fallback."""
    fallback = [block_bbox] if block_bbox else []
    if (not words) and block_bbox and surface:
        interp = interpolate_entity_box(block_bbox, block_text, surface)
        return interp or fallback
    if not words or not block_bbox or not surface:
        return fallback
    in_block = _reading_order(
        [w for w in words if int(w.get("page", page)) == int(page)
         and _center_inside(w.get("bbox") or [], block_bbox)])
    if not in_block:
        return fallback

    target = _norm(surface)
    if not target:
        return fallback
    # find the shortest contiguous run of words whose joined text contains target
    n = len(in_block)
    best: list[dict[str, Any]] | None = None
    for i in range(n):
        joined = ""
        for j in range(i, n):
            joined += _norm(in_block[j]["text"])
            if target in joined:
                run = in_block[i:j + 1]
                if best is None or len(run) < len(best):
                    best = run
                break
            if len(joined) > len(target) + 40:      # this start can't tighten further
                break
    if best is None:
        return fallback
    return _merge_per_line(best)
