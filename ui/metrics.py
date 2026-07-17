"""
SME Review Portal — metric computation.

Pure functions implementing the formulas in FR-6 (per-doc) and FR-7.1
(aggregate) of sme_capture_requirements.md.

Two entry points:
  compute_per_doc_metrics(state, fields_total) -> Metrics
    - Counts action types from a single ReviewState
    - Uses only LATEST un-superseded actions (DR-2.3)
    - Returns Metrics populated with counters + 3 ratios

  aggregate_metrics(per_doc: list[Metrics]) -> Metrics
    - SUMS counters across docs, THEN computes ratios (FR-7.1)
    - Never means-of-ratios (would bias small docs vs large ones)

Ratio formulas (FR-6 v2 — confusion-matrix model):
  A = accept_count, I = implicit_accept_count
  C = correct_count
  R = reject_count + reject_record_count
  M = add_missing_count
  Fr = fields_reviewed  = A + C + R + M         (explicit touches only)
  Ft = fields_total     = number of reviewable fields in extraction_v2.json

  Confusion-matrix interpretation:
    TP = accept + implicit_accept (extractor was exactly right)
    FP = correct + reject + reject_record (extractor produced wrong output — wrong value OR shouldn't exist)
    FN = add_missing (extractor missed a record)

  accuracy  = TP / (TP + FP + FN) = (A + I) / (A + I + C + R + M)   # overall correctness
  precision = TP / (TP + FP)      = (A + I) / (A + I + C + R)       # of what we produced, how much was right
  recall    = TP / (TP + FN)      = (A + I) / (A + I + M)           # of what should exist, how much did we find

  Rationale for switching from Q10-B to this model (user feedback):
    Q10-B credited corrections in precision numerator, which produced misleading
    "100% precision" readings when the SME had made corrections. Under the
    confusion-matrix model, corrections count as FP (extractor was wrong),
    matching clinical intuition.

Edge cases:
  * Denominator == 0 → return 0.0 for that ratio. Never raise, never NaN.

Design constraints:
  * Pure stdlib (NFR-1.2).
  * No I/O. Pure functions of the inputs.
"""
from __future__ import annotations

from schemas import ActionType, Metrics, ReviewState


def _safe_ratio(numerator: int, denominator: int) -> float:
    """Divide with a zero-denominator guard. Returns 0.0 for 0/0 rather than raising."""
    if denominator == 0:
        return 0.0
    return numerator / denominator


def compute_per_doc_metrics(state: ReviewState, fields_total: int) -> Metrics:
    """
    Compute per-doc metrics from a ReviewState.

    Only counts LATEST un-superseded actions (DR-2.3). If the SME
    accepted a field, then later corrected it, only the correction
    counts — the accept was superseded.

    fields_total is the total number of reviewable fields in the
    extraction (from generate_field_ids); needed for the fields_total
    field of the Metrics record. Ratios do NOT depend on fields_total.
    """
    # Take the latest action per field so superseded actions are ignored.
    latest_by_field = state.latest_action_per_field()

    counters = {t: 0 for t in ActionType}
    for action in latest_by_field.values():
        counters[action.action] += 1

    A = counters[ActionType.ACCEPT]
    I_ = counters[ActionType.IMPLICIT_ACCEPT]
    C = counters[ActionType.CORRECT]
    R_field = counters[ActionType.REJECT]
    R_record = counters[ActionType.REJECT_RECORD]
    R = R_field + R_record
    M = counters[ActionType.ADD_MISSING]

    fields_reviewed = A + C + R + M   # explicit touches; excludes implicit

    # Confusion-matrix formulas (v2 — see module docstring for rationale).
    TP = A + I_
    FP = C + R
    FN = M

    accuracy  = _safe_ratio(TP, TP + FP + FN)
    precision = _safe_ratio(TP, TP + FP)
    recall    = _safe_ratio(TP, TP + FN)

    return Metrics(
        accept_count=A,
        correct_count=C,
        reject_count=R_field,
        reject_record_count=R_record,
        add_missing_count=M,
        implicit_accept_count=I_,
        fields_reviewed=fields_reviewed,
        fields_total=fields_total,
        accuracy=accuracy,
        precision=precision,
        recall=recall,
    )


def aggregate_metrics(per_doc: list[Metrics]) -> Metrics:
    """
    Aggregate across docs by SUMMING raw counters, then recomputing ratios
    on the summed totals.

    NOT a mean of per-doc ratios (per FR-7.1). Consider:
      doc1: 90 accept, 10 reject → accuracy = 90%
      doc2:  0 accept, 10 reject → accuracy =  0%
      MEAN of ratios: 45%
      SUM  of counters: 90 / 110 = 81.8%   ← we use this (weights by doc size)
    """
    A = sum(m.accept_count for m in per_doc)
    C = sum(m.correct_count for m in per_doc)
    R_field = sum(m.reject_count for m in per_doc)
    R_record = sum(m.reject_record_count for m in per_doc)
    R = R_field + R_record
    M = sum(m.add_missing_count for m in per_doc)
    I_ = sum(m.implicit_accept_count for m in per_doc)
    fields_reviewed = sum(m.fields_reviewed for m in per_doc)
    fields_total = sum(m.fields_total for m in per_doc)

    # Same confusion-matrix formulas as per-doc; applied on summed counters
    # per FR-7.1 (sum of counters, not mean of per-doc ratios).
    TP = A + I_
    FP = C + R
    FN = M

    accuracy  = _safe_ratio(TP, TP + FP + FN)
    precision = _safe_ratio(TP, TP + FP)
    recall    = _safe_ratio(TP, TP + FN)

    return Metrics(
        accept_count=A,
        correct_count=C,
        reject_count=R_field,
        reject_record_count=R_record,
        add_missing_count=M,
        implicit_accept_count=I_,
        fields_reviewed=fields_reviewed,
        fields_total=fields_total,
        accuracy=accuracy,
        precision=precision,
        recall=recall,
    )
