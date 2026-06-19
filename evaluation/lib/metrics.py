"""Compute classification-style precision/recall/F1 at three levels:
   - per-field (one entry per column)
   - per-section (variants / biomarkers / overall)
   - per-row (matched / missing / spurious)

A WRONG cell counts as BOTH a FP and a FN at field level (the classifier said
something AND missed the truth at the same coordinate). This matches the
sklearn convention for multi-class classification when classes don't overlap.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from evaluation.lib.compare_cells import CellVerdict, TP, WRONG, FN, FP, TN, compare
from evaluation.lib.match_rows import RowPair


def _safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0


def _prf(tp: int, fp: int, fn: int) -> dict[str, float]:
    p = _safe_div(tp, tp + fp)
    r = _safe_div(tp, tp + fn)
    f1 = _safe_div(2 * p * r, p + r)
    return {"precision": round(p, 3), "recall": round(r, 3), "f1": round(f1, 3)}


def compute(pairs: list[RowPair], columns: list[dict]) -> dict[str, Any]:
    """Produce a metrics dict (machine-readable, ready for metrics.json)."""
    # Row-level buckets
    sec_rows: dict[str, dict[str, int]] = defaultdict(
        lambda: {"matched": 0, "missing": 0, "spurious": 0})
    # Field-level buckets
    field_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {"tp": 0, "fp": 0, "fn": 0, "wrong": 0})
    # Per-cell verdicts for reports
    cell_verdicts: list[dict[str, Any]] = []

    column_headers_ordered = [c.get("header") for c in columns if c.get("header")]
    # Defensive dedup — current field_map has unique headers, but if a future
    # YAML reintroduces a duplicate, keep the FIRST occurrence (matches the
    # rule in load_extraction._cells_from_record / load_ground_truth).
    seen: set = set()
    unique_headers: list[str] = []
    for h in column_headers_ordered:
        if h not in seen:
            unique_headers.append(h)
            seen.add(h)

    for pair in pairs:
        sec = pair.section
        if pair.gt is not None and pair.extracted is not None:
            sec_rows[sec]["matched"] += 1
            for header in unique_headers:
                gt_v = pair.gt.cells.get(header)
                ex_v = pair.extracted.cells.get(header)
                v = compare(gt_v, ex_v)
                cell_verdicts.append({
                    "section": sec, "identity": pair.identity, "column": header,
                    "verdict": v.verdict, "gt": v.gt_raw, "extracted": v.ex_raw,
                    "gt_canon": v.gt_canon, "ex_canon": v.ex_canon,
                })
                if v.verdict == TP:
                    field_counts[header]["tp"] += 1
                elif v.verdict == FN:
                    field_counts[header]["fn"] += 1
                elif v.verdict == FP:
                    field_counts[header]["fp"] += 1
                elif v.verdict == WRONG:
                    field_counts[header]["wrong"] += 1
                # TN ignored at field level
        elif pair.gt is not None and pair.extracted is None:
            sec_rows[sec]["missing"] += 1
            # Every non-empty GT cell on a missing row → FN
            for header in unique_headers:
                gt_v = pair.gt.cells.get(header)
                if gt_v is None or (isinstance(gt_v, str) and not gt_v.strip()):
                    continue
                field_counts[header]["fn"] += 1
                cell_verdicts.append({
                    "section": sec, "identity": pair.identity, "column": header,
                    "verdict": FN, "gt": str(gt_v), "extracted": "",
                    "gt_canon": "", "ex_canon": "",
                })
        elif pair.gt is None and pair.extracted is not None:
            sec_rows[sec]["spurious"] += 1
            for header in unique_headers:
                ex_v = pair.extracted.cells.get(header)
                if ex_v is None or (isinstance(ex_v, str) and not ex_v.strip()):
                    continue
                field_counts[header]["fp"] += 1
                cell_verdicts.append({
                    "section": sec, "identity": pair.identity, "column": header,
                    "verdict": FP, "gt": "", "extracted": str(ex_v),
                    "gt_canon": "", "ex_canon": "",
                })

    # Row-level P/R/F1.
    row_level: dict[str, Any] = {}
    total_matched, total_missing, total_spurious = 0, 0, 0
    for sec in ("variants", "biomarkers"):
        m = sec_rows[sec]["matched"]
        miss = sec_rows[sec]["missing"]
        spur = sec_rows[sec]["spurious"]
        total_matched += m
        total_missing += miss
        total_spurious += spur
        row_level[sec] = {
            "gt": m + miss, "extracted": m + spur,
            "matched": m, "missing": miss, "spurious": spur,
            **_prf(m, spur, miss),
        }
    row_level["overall"] = {
        "gt": total_matched + total_missing,
        "extracted": total_matched + total_spurious,
        "matched": total_matched, "missing": total_missing, "spurious": total_spurious,
        **_prf(total_matched, total_spurious, total_missing),
    }

    # Field-level P/R/F1. WRONG counts as both FP (extracted said X) and FN (missed truth).
    field_level: dict[str, Any] = {}
    total = {"tp": 0, "fp": 0, "fn": 0, "wrong": 0}
    for header in unique_headers:
        c = field_counts[header]
        eff_fp = c["fp"] + c["wrong"]
        eff_fn = c["fn"] + c["wrong"]
        field_level[header] = {
            **c,
            "precision": _prf(c["tp"], eff_fp, eff_fn)["precision"],
            "recall":    _prf(c["tp"], eff_fp, eff_fn)["recall"],
            "f1":        _prf(c["tp"], eff_fp, eff_fn)["f1"],
        }
        for k in total:
            total[k] += c[k]

    # Overall (aggregate over fields).
    eff_fp_total = total["fp"] + total["wrong"]
    eff_fn_total = total["fn"] + total["wrong"]
    overall = {
        **total,
        "precision": _prf(total["tp"], eff_fp_total, eff_fn_total)["precision"],
        "recall":    _prf(total["tp"], eff_fp_total, eff_fn_total)["recall"],
        "f1":        _prf(total["tp"], eff_fp_total, eff_fn_total)["f1"],
    }

    return {
        "row_level": row_level,
        "field_level": field_level,
        "overall": overall,
        "_cell_verdicts": cell_verdicts,    # internal — used by render_report
    }
