"""Per-cell comparison — the 5-line strict-on-canonical comparator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from evaluation.lib.canonical import canonical


# Verdict tags.
TP    = "TP"        # both non-empty, canonical-equal
WRONG = "WRONG"     # both non-empty, canonical-not-equal
FN    = "FN"        # GT non-empty, extraction empty
FP    = "FP"        # GT empty,     extraction non-empty
TN    = "TN"        # both empty


def _is_empty(v: Any) -> bool:
    """A cell is empty when it's None / blank string / NaN-string."""
    if v is None:
        return True
    s = str(v).strip()
    if not s:
        return True
    if s.lower() in {"nan", "none", "null", "(none)"}:
        return True
    return False


@dataclass
class CellVerdict:
    verdict: str       # TP | WRONG | FN | FP | TN
    gt_raw: str
    ex_raw: str
    gt_canon: str
    ex_canon: str


def compare(gt: Any, ex: Any) -> CellVerdict:
    """Strict comparison after running both sides through `canonical()`.

    Why canonical? Because GT is SME-authored (often clinical-canonical: "MSI",
    "HER2") while extraction is verbatim from source ("MICROSATELLITE
    INSTABILITY", "ERBB2"). The canonicalizer levels the surface-form
    difference using EXISTING pipeline infra; everything else is strict.
    """
    gt_empty = _is_empty(gt)
    ex_empty = _is_empty(ex)

    if gt_empty and ex_empty:
        return CellVerdict(TN, "", "", "", "")
    if not gt_empty and ex_empty:
        return CellVerdict(FN, str(gt), "", canonical(gt), "")
    if gt_empty and not ex_empty:
        return CellVerdict(FP, "", str(ex), "", canonical(ex))

    gt_c = canonical(gt)
    ex_c = canonical(ex)
    if gt_c == ex_c:
        return CellVerdict(TP, str(gt), str(ex), gt_c, ex_c)
    return CellVerdict(WRONG, str(gt), str(ex), gt_c, ex_c)
