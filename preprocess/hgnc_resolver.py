"""
HGNC resolver — gene-symbol canonicalization with bounded, FLAGGED OCR repair.

Policy (Phase 2 OCR decision, Sri 2026-05-23):
  - EXACT alias match (case/punctuation-insensitive: "JAK-2" → "JAK2",
    "HER2/NEU" → "ERBB2") → canonical, not a correction.
  - FUZZY match against the alias table within a tight edit-distance bound
    ("JAKZ" → "JAK2") → canonical BUT flagged (`fuzzy=True`); the caller stamps
    provenance `type: derived` and keeps the original surface. This is a
    correction-with-evidence (a finite reference table), never a free guess.
  - AMBIGUOUS (≥2 distinct canonicals tie at the minimum distance, e.g. an OCR
    "XRAS" equidistant from KRAS and NRAS) → status="ambiguous", canonical=None.
    We do NOT pick — it becomes an uncertain → SME case downstream.
  - UNKNOWN (no exact, nothing within bound) → status="unknown", canonical=None.

The verbatim surface is ALWAYS preserved by the caller; this tool never mutates
the page text, it only proposes a canonical identity + a status flag.

Table: `config/data/hgnc_aliases.tsv` (alias<TAB>approved_symbol). Refresh with
`make fetch-hgnc` (downloads the HGNC complete set; offline seed shipped for
dev). No network at runtime.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_TSV = "config/data/hgnc_aliases.tsv"

# Tight edit-distance bound: 1 for short symbols (≤5 chars), 2 for longer. Kept
# conservative on purpose — fuzzy repair must not introduce errors into ground
# truth. Ambiguous ties never auto-resolve.
def _max_distance(key: str) -> int:
    return 1 if len(key) <= 5 else 2


_KEY_STRIP = re.compile(r"[^A-Z0-9]")


def _norm_key(symbol: str) -> str:
    """Normalize a symbol for matching: uppercase, drop spaces/hyphens/slashes/
    dots. 'JAK-2' → 'JAK2', 'her2/neu' → 'HER2NEU'."""
    return _KEY_STRIP.sub("", symbol.upper())


def _levenshtein(a: str, b: str, cap: int) -> int:
    """Bounded Levenshtein. Returns the true distance, or cap+1 if it exceeds
    `cap` (early-exit for speed)."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        row_min = i
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            cur.append(v)
            row_min = min(row_min, v)
        if row_min > cap:
            return cap + 1
        prev = cur
    return prev[-1]


@lru_cache(maxsize=4)
def _load_table(tsv_path: str) -> tuple[dict[str, str], tuple[tuple[str, str], ...]]:
    """Load the alias TSV. Returns (exact_index, fuzzy_pairs) where
    exact_index maps norm_key → canonical and fuzzy_pairs is the distinct
    (norm_key, canonical) list for fuzzy search."""
    p = Path(tsv_path)
    if not p.exists():
        raise FileNotFoundError(f"HGNC alias TSV not found: {p}")
    exact: dict[str, str] = {}
    for ln, line in enumerate(p.read_text(encoding="utf-8").splitlines()):
        if not line.strip() or ln == 0 and line.lower().startswith("alias"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        alias, canonical = parts[0].strip(), parts[1].strip()
        if not alias or not canonical:
            continue
        exact[_norm_key(alias)] = canonical
    pairs = tuple(sorted(exact.items()))
    return exact, pairs


def hgnc_normalize(symbol: str, *, tsv_path: str = DEFAULT_TSV) -> dict:
    """Canonicalize a gene symbol. See module docstring for the policy.

    Returns:
        {
          "input": str,                 # original surface, preserved
          "normalized_key": str,
          "canonical": str | None,      # None when ambiguous/unknown
          "status": "exact"|"fuzzy"|"ambiguous"|"unknown",
          "fuzzy": bool,                # True ⇒ flag in provenance as derived
          "candidates": list[str],      # distinct canonicals considered (fuzzy)
        }
    """
    raw = (symbol or "").strip()
    key = _norm_key(raw)
    exact, pairs = _load_table(tsv_path)

    if not key:
        return {"input": raw, "normalized_key": key, "canonical": None,
                "status": "unknown", "fuzzy": False, "candidates": []}

    if key in exact:
        return {"input": raw, "normalized_key": key, "canonical": exact[key],
                "status": "exact", "fuzzy": False, "candidates": [exact[key]]}

    # Fuzzy: find the minimum-distance candidates within the bound.
    cap = _max_distance(key)
    best_dist = cap + 1
    best_canon: dict[str, int] = {}   # canonical → min distance seen
    for k, canonical in pairs:
        d = _levenshtein(key, k, cap)
        if d <= cap:
            if d < best_dist:
                best_dist = d
                best_canon = {canonical: d}
            elif d == best_dist:
                best_canon.setdefault(canonical, d)

    distinct = sorted(best_canon)
    if len(distinct) == 1:
        return {"input": raw, "normalized_key": key, "canonical": distinct[0],
                "status": "fuzzy", "fuzzy": True, "candidates": distinct}
    if len(distinct) >= 2:
        return {"input": raw, "normalized_key": key, "canonical": None,
                "status": "ambiguous", "fuzzy": True, "candidates": distinct}
    return {"input": raw, "normalized_key": key, "canonical": None,
            "status": "unknown", "fuzzy": False, "candidates": []}
