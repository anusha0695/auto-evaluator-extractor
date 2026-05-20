"""
BboxSynthesizer — fallback bbox extraction when DocAI doesn't return them.

The pinned Layout Parser v1.6 processor doesn't honor
`process_options.layout_config.return_bounding_boxes=True` — the response
arrives without spatial info. This module fills the gap by locating each
block's text in the PDF via pdfplumber and synthesizing a bounding box from
the matching word positions.

Returned bboxes are in DocAI-compatible normalized coordinates
(`[x0, y0, x1, y1]` ∈ [0, 1], origin top-left), so they slot straight into
`BlockInfo.bbox` without UI rewrites.

Usage:

    synth = BboxSynthesizer()
    blocks = synth.synthesize(pdf_bytes, blocks)  # mutates bbox in place

Design notes:
- Best-effort. A block whose text we can't locate keeps its (possibly
  empty) existing bbox.
- We try a few matching strategies in order: exact run match, partial
  match on first N words, partial match on last N words. This handles
  DocAI's habit of normalizing whitespace and dropping trailing punctuation.
- pdfplumber is pure-python (depends on pdfminer.six); no native libs.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from core.state import BlockInfo

logger = logging.getLogger(__name__)


# Tokenization regex — whitespace plus common punctuation we want to ignore
# when comparing DocAI block text against pdfplumber's word list.
_WS_PUNCT = re.compile(r"[\s   ​  ‌"
                       r"‍⁠﻿\.\,\:\;\!\?\(\)\[\]\{\}"
                       r"\"\'\/\\\-–—]+")


def _norm(s: str) -> str:
    """Normalize for matching: lower, collapse whitespace + punctuation."""
    return _WS_PUNCT.sub(" ", (s or "").lower()).strip()


def _first_n_words(s: str, n: int = 6) -> str:
    return " ".join(_norm(s).split()[:n])


def _last_n_words(s: str, n: int = 6) -> str:
    return " ".join(_norm(s).split()[-n:])


class BboxSynthesizer:
    """Synthesizes normalized bboxes for blocks from pdfplumber word positions."""

    def __init__(self) -> None:
        self._pdfplumber: Any = None

    def _import_pdfplumber(self) -> Any:
        if self._pdfplumber is None:
            try:
                import pdfplumber
            except ImportError as exc:
                raise RuntimeError(
                    "pdfplumber is required for bbox synthesis. "
                    "Add 'pdfplumber' to requirements.txt."
                ) from exc
            self._pdfplumber = pdfplumber
        return self._pdfplumber

    def synthesize(
        self, pdf_bytes: bytes, blocks: list[BlockInfo]
    ) -> list[BlockInfo]:
        """Fill bbox on any block where it's empty. Returns the same list
        (mutated in place) for convenience.

        Each block keeps its existing bbox if non-empty. Otherwise we try
        to locate its text in the PDF page and synthesize one.
        """
        if not blocks:
            return blocks

        # Only blocks that need a bbox; group by page for one open() per page.
        needs_bbox: dict[int, list[BlockInfo]] = {}
        for b in blocks:
            if not (b.get("bbox") or []):
                pnum = int(b.get("page_number") or 1)
                needs_bbox.setdefault(pnum, []).append(b)

        if not needs_bbox:
            return blocks

        pdfplumber = self._import_pdfplumber()
        import io as _io

        filled = 0
        with pdfplumber.open(_io.BytesIO(pdf_bytes)) as pdf:
            for pnum, page_blocks in needs_bbox.items():
                if pnum < 1 or pnum > len(pdf.pages):
                    continue
                page = pdf.pages[pnum - 1]
                page_w = float(page.width or 0) or 1.0
                page_h = float(page.height or 0) or 1.0
                words = page.extract_words(
                    use_text_flow=True,
                    keep_blank_chars=False,
                    extra_attrs=[],
                ) or []
                if not words:
                    continue

                # Build a flat lowercased token list with their bboxes for matching.
                tokens = [_norm(w["text"]) for w in words]
                tok_index = " ".join(tokens)  # for substring scanning
                # Map char-position-in-tok_index → word index
                char_to_word: list[int] = []
                pos = 0
                for i, t in enumerate(tokens):
                    char_to_word.extend([i] * len(t))
                    if i < len(tokens) - 1:
                        char_to_word.append(i)  # the joining space
                        pos += 1
                    pos += len(t)

                for blk in page_blocks:
                    bbox = self._locate_block(
                        blk.get("text") or "", tok_index, char_to_word, words,
                        page_w, page_h,
                    )
                    if bbox:
                        blk["bbox"] = bbox
                        filled += 1

        if filled:
            logger.info(
                "BboxSynthesizer: synthesized %d/%d missing bboxes from "
                "pdfplumber word positions.", filled,
                sum(len(v) for v in needs_bbox.values()),
            )
        return blocks

    # ------------------------------------------------------------------
    # Internal: locate a block's text in the page's word stream
    # ------------------------------------------------------------------

    @staticmethod
    def _locate_block(
        block_text: str,
        tok_index: str,
        char_to_word: list[int],
        words: list[dict],
        page_w: float,
        page_h: float,
    ) -> list[float] | None:
        """Try exact match, then prefix, then suffix. Returns normalized bbox
        spanning the matched word range, or None if no match."""
        target = _norm(block_text)
        if not target or not tok_index:
            return None

        # Strategy 1: exact substring match
        idx = tok_index.find(target)
        if idx >= 0 and idx < len(char_to_word):
            start_word = char_to_word[idx]
            end_char = min(idx + len(target) - 1, len(char_to_word) - 1)
            end_word = char_to_word[end_char]
            return _word_range_to_bbox(words, start_word, end_word, page_w, page_h)

        # Strategy 2: first-N-words prefix match (block was truncated, or
        # DocAI added a trailing run we don't see in pdfplumber)
        prefix = _first_n_words(block_text, 6)
        if prefix:
            idx = tok_index.find(prefix)
            if idx >= 0 and idx < len(char_to_word):
                start_word = char_to_word[idx]
                # extend forward by the number of words in the full target
                num_words = len(target.split())
                end_word = min(start_word + num_words - 1, len(words) - 1)
                return _word_range_to_bbox(words, start_word, end_word,
                                           page_w, page_h)

        # Strategy 3: last-N-words suffix match
        suffix = _last_n_words(block_text, 6)
        if suffix:
            idx = tok_index.rfind(suffix)
            if idx >= 0 and idx < len(char_to_word):
                end_char = min(idx + len(suffix) - 1, len(char_to_word) - 1)
                end_word = char_to_word[end_char]
                num_words = len(target.split())
                start_word = max(end_word - num_words + 1, 0)
                return _word_range_to_bbox(words, start_word, end_word,
                                           page_w, page_h)

        return None


def _word_range_to_bbox(
    words: list[dict], i0: int, i1: int, page_w: float, page_h: float,
) -> list[float]:
    """Return a normalized [x0,y0,x1,y1] spanning words[i0..i1] inclusive."""
    sub = words[i0: i1 + 1]
    if not sub:
        return []
    x0 = min(float(w["x0"]) for w in sub) / page_w
    y0 = min(float(w["top"]) for w in sub) / page_h
    x1 = max(float(w["x1"]) for w in sub) / page_w
    y1 = max(float(w["bottom"]) for w in sub) / page_h
    # Clamp to [0,1] in case of stretchers / annotations
    return [max(0.0, x0), max(0.0, y0), min(1.0, x1), min(1.0, y1)]
