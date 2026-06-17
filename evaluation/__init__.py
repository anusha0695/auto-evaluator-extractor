"""Evaluation module — compares extraction_production.json against SME-authored ground-truth XLSX.

Entry point:
    python -m evaluation --doc <doc_id>

Reads:
    ground_truth/<doc_id>.xlsx                                    (SME-authored)
    local_runs/artifacts/<doc_id>/extraction_production.json      (pipeline output)

Writes:
    local_runs/evaluations/<doc_id>/metrics.json
    local_runs/evaluations/<doc_id>/classification_report.txt
    local_runs/evaluations/<doc_id>/mismatches.xlsx               (Wrong/Missing/Spurious sheets)
    local_runs/evaluations/<doc_id>/summary.md
"""
