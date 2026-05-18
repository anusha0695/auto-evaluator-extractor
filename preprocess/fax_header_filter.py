"""
FaxHeaderFilter — deterministic regex stripper for fax-transport noise.

Driven by `demo.pdf` (real NeoGenomics fax) and the prompt-design rules in
`ground_truth/phase1/README.md` § Key findings. The fax band sits at the top
of page 1 (and a one-liner on every subsequent page) and consists of metadata
added by the *receiving* fax machine — it is not part of the report and must
be stripped before any LLM extractor sees the text.

Three signature patterns (each individually sufficient to flag a block):

  1. `** INBOUND NOTIFICATION : FAX RECEIVED SUCCESSFULLY **`
  2. A multi-token header line containing TIME RECEIVED, REMOTE CSID,
     DURATION, PAGES, and STATUS (typically a one-row table at the top).
  3. The CSID timestamp line of the form
     `MM.DD.YYYY HH:MM:SS Page: <N> of <M> Sender: <text> Recipient: +<digits>`
     — note the DOT separators in the date (fax CSIDs use dots, not slashes).
     This is the per-page marker that recurs on every page.

The filter is run as the second step of preprocessing (after DocAI parsing
and before Block Profiler). Two effects:

  - Per-page text: any line matching one of the signatures is removed; the
    rest of the page text is left untouched.
  - Surviving blocks: those overlapping a stripped region are marked with
    `is_fax_noise=True` so Block Profiler can assign `text_role:
    fax_transport_noise`.

The filter is purely deterministic — no LLM call. Speed is not a concern;
correctness on `demo.pdf` is.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from core.observability import trace
from core.state import BlockInfo, PageText

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Regex signatures
# ---------------------------------------------------------------------------

# Banner line.
_BANNER = re.compile(
    r"""\*{2,}\s*INBOUND\s+NOTIFICATION\s*:\s*FAX\s+RECEIVED\s+SUCCESSFULLY\s*\*{2,}""",
    flags=re.IGNORECASE,
)

# Multi-column header (TIME RECEIVED ... REMOTE CSID ... DURATION ... PAGES ... STATUS).
# Matches any line that contains 3+ of these tokens in order. Robust to spacing.
_COL_HEADER = re.compile(
    r"""(?ix)
    TIME\s+RECEIVED .{0,80}? REMOTE\s+CSID .{0,80}? DURATION
    """,
)

# CSID timestamp line. The discriminator vs legitimate report dates is the
# DOT-separated date (08.27.2025) and the "Page: N of M Sender: ... Recipient:"
# trailer.
_CSID_TIMESTAMP = re.compile(
    r"""
    \b\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}:\d{2}\b   # MM.DD.YYYY HH:MM:SS
    .{0,30}?                                       # optional gap
    Page:\s*\d+\s+of\s+\d+                         # Page: N of M
    .{0,200}?                                      # sender / recipient body
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)

# The header CSID-status row that often appears as a continuation:
#     "August 26, 2025 at 4:42:57 PM PDT  ...  REMOTE CSID NeoGenomics ..."
# We catch it via the multi-column header pattern above.

# A trailing fragment that sometimes appears on its own line:
_PAGE_OF_BANNER = re.compile(
    r"""\bPage:\s*\d+\s+of\s+\d+\s+Sender:\s+[^\n\r]{1,80}\s+Recipient:\s*\+?\d{6,}\b""",
    flags=re.IGNORECASE,
)

# The data row paired with _COL_HEADER. On `demo.pdf` DocAI extracts it as:
#   "August 26, 2025 at 4:42:57 PM PDT      NeoGenomics       91   2   Received"
# Discriminator: "<date> at <HH:MM:SS>" + a trailing "Received" status. Real
# report date lines use ":" or "/" separators and don't say " at " — so this
# pattern won't false-positive on Collection_Date / Received_Date / Report_Date.
_STATUS_ROW = re.compile(
    r"""
    \b\d{4}\s+at\s+\d{1,2}:\d{2}(?::\d{2})?\s*  # ", YYYY at HH:MM[:SS]"
    (?:AM|PM)?\s*                                # optional AM/PM
    (?:[A-Z]{2,4})?\s*                           # optional timezone (PDT, ET, ...)
    .{1,150}?                                    # any column gap
    \b(?:Received|Success(?:fully)?|OK)\b        # trailing status word
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)

# Combined: a line is fax noise if any of these match.
_LINE_PATTERNS = [_BANNER, _COL_HEADER, _CSID_TIMESTAMP, _PAGE_OF_BANNER, _STATUS_ROW]


# ---------------------------------------------------------------------------
# FaxHeaderFilter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FaxFilterResult:
    """Returned by filter()."""

    pages: list[PageText]
    blocks: list[BlockInfo]
    fax_noise_block_ids: set[str]
    pages_modified: int
    blocks_flagged: int


class FaxHeaderFilter:
    """Strip fax-transport bands from per-page text and flag overlapping blocks."""

    @trace("preprocess.fax_header_filter.filter")
    def filter(
        self,
        *,
        pages: list[PageText],
        blocks: list[BlockInfo],
    ) -> FaxFilterResult:
        """Run the filter. Returns cleaned pages, the original blocks
        (unchanged in position/content), and the set of block_ids whose text
        intersects a stripped fragment."""
        cleaned_pages: list[PageText] = []
        pages_modified = 0
        fax_noise_block_ids: set[str] = set()

        # Build a quick lookup: page_number → list[block]
        blocks_by_page: dict[int, list[BlockInfo]] = {}
        for b in blocks:
            blocks_by_page.setdefault(b.get("page_number") or 1, []).append(b)

        for p in pages:
            page_number = p.get("page_number") or 1
            text = p.get("text", "") or ""
            cleaned_text, removed_spans = self._strip_fax_noise(text)

            modified = bool(removed_spans)
            if modified:
                pages_modified += 1

            cleaned_pages.append(
                PageText(
                    page_number=page_number,
                    text=cleaned_text,
                    block_ids_on_page=list(p.get("block_ids_on_page", []) or []),
                )
            )

            # Tag any block whose text intersects a stripped span.
            for block in blocks_by_page.get(page_number, []):
                btext = block.get("text", "") or ""
                if not btext.strip():
                    continue
                if self._block_text_is_fax_noise(btext):
                    fax_noise_block_ids.add(block.get("block_id", ""))

        logger.info(
            "FaxHeaderFilter: pages_modified=%d, blocks_flagged=%d",
            pages_modified,
            len(fax_noise_block_ids),
        )
        return FaxFilterResult(
            pages=cleaned_pages,
            blocks=blocks,                       # blocks untouched
            fax_noise_block_ids=fax_noise_block_ids,
            pages_modified=pages_modified,
            blocks_flagged=len(fax_noise_block_ids),
        )

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    @staticmethod
    def _strip_fax_noise(text: str) -> tuple[str, list[tuple[int, int]]]:
        """Remove fax-noise spans from text. Returns (cleaned_text, spans).

        Strategy: line-by-line. A line matching ANY pattern is dropped. The
        spans are returned in original-text coords (start, end) for blocks
        to use when deciding what to flag.
        """
        if not text:
            return text, []

        # Compute line ranges in the original text
        kept_lines: list[str] = []
        removed_spans: list[tuple[int, int]] = []
        idx = 0
        for line in text.splitlines(keepends=True):
            line_start = idx
            line_end = idx + len(line)
            idx = line_end
            stripped = line.strip()
            if not stripped:
                kept_lines.append(line)
                continue
            if any(p.search(stripped) for p in _LINE_PATTERNS):
                removed_spans.append((line_start, line_end))
                continue
            kept_lines.append(line)
        cleaned = "".join(kept_lines)
        return cleaned, removed_spans

    @staticmethod
    def _block_text_is_fax_noise(block_text: str) -> bool:
        """True if any pattern matches inside the block's own text."""
        if not block_text:
            return False
        s = block_text.strip()
        return any(p.search(s) for p in _LINE_PATTERNS)
