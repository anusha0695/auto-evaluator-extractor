"""
Synonym + quantity normalizers (deterministic, offline).

  - biomarker_normalize(name)   → canonical non-gene biomarker label (dedup key).
  - method_normalize(method)    → canonical assay-method label (dedup key).
  - normalize_quantity(value)   → canonical numeric for the EQUIVALENCE check
                                  only (VAF "12%" ↔ "0.12"; "2.3 cm" ↔ "23 mm").

Cardinal rule: these produce a comparison key; the **verbatim surface is always
kept** by the caller. `normalize_quantity` never repairs OCR (Tier-2) — a
malformed value returns canonical=None and is left verbatim / escalated.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_BIOMARKER_YAML = "config/data/biomarker_synonyms.yaml"
_METHOD_YAML = "config/data/method_synonyms.yaml"


@lru_cache(maxsize=8)
def _load_synonyms(path: str, top_key: str) -> tuple[dict[str, str], tuple[str, ...]]:
    """Return (synonym_lower → canonical, canonicals tuple)."""
    import yaml
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"synonym file not found: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    table = raw.get(top_key, {}) or {}
    rev: dict[str, str] = {}
    for canonical, syns in table.items():
        rev[canonical.lower()] = canonical          # canonical maps to itself
        for s in (syns or []):
            rev[str(s).lower()] = canonical
    return rev, tuple(table.keys())


def _normalize_against(value: str, path: str, top_key: str) -> dict:
    raw = (value or "").strip()
    rev, _ = _load_synonyms(path, top_key)
    key = raw.lower()
    if key in rev:
        return {"input": raw, "canonical": rev[key], "matched": True}
    return {"input": raw, "canonical": raw, "matched": False}


def biomarker_normalize(name: str) -> dict:
    """Canonicalize a non-gene biomarker name (PD-L1 ↔ CD274 ↔ pdl1)."""
    return _normalize_against(name, _BIOMARKER_YAML, "biomarkers")


def method_normalize(method: str) -> dict:
    """Canonicalize an assay method (Immunohistochemistry → IHC)."""
    return _normalize_against(method, _METHOD_YAML, "methods")


# ---------------------------------------------------------------------------
# Quantity normalization (equivalence key only)
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"[-+]?\d*\.?\d+")
# length unit → millimetres
_LEN_TO_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0}


def normalize_quantity(value: str, unit: str | None = None) -> dict:
    """Map a value (+optional unit) to a canonical numeric for equivalence.

    Rules:
      - percent ("12%") → fraction 0.12 (kind="fraction"); a bare "0.12" also
        → 0.12, so "12%" and "0.12" compare equal.
      - length ("2.3 cm", "23 mm") → millimetres (kind="length_mm").
      - bare number → float (kind="number").
      - unparseable → canonical=None (left verbatim; never repaired).

    Returns {input, canonical_value, canonical_unit, kind, reason}.
    """
    raw = (value or "").strip()
    text = raw if unit is None else f"{raw} {unit}".strip()
    low = text.lower()

    m = _NUM_RE.search(low)
    if not m:
        return {"input": raw, "canonical_value": None, "canonical_unit": None,
                "kind": None, "reason": "no numeric token (left verbatim)"}
    num = float(m.group(0))

    if "%" in low or (unit and unit.strip() == "%"):
        return {"input": raw, "canonical_value": num / 100.0, "canonical_unit": "fraction",
                "kind": "fraction", "reason": "percent → fraction"}

    for u, factor in _LEN_TO_MM.items():
        if re.search(r"(?<![a-z])" + u + r"(?![a-z])", low):
            return {"input": raw, "canonical_value": num * factor, "canonical_unit": "mm",
                    "kind": "length_mm", "reason": f"{u} → mm"}

    # bare number: if it's a 0..1 fraction we still expose it as a fraction so a
    # raw "0.12" compares equal to a "12%".
    if 0.0 <= num <= 1.0:
        return {"input": raw, "canonical_value": num, "canonical_unit": "fraction",
                "kind": "fraction", "reason": "bare fraction"}
    return {"input": raw, "canonical_value": num, "canonical_unit": None,
            "kind": "number", "reason": "bare number"}
