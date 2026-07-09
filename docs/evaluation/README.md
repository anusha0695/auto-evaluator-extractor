# Evaluation Module — Verifiable Ground Truth Comparison

This module compares the extractor's `extraction_production.json` against an
SME-authored ground-truth XLSX and produces color-coded review workbooks,
per-cell verdicts, precision/recall/F1 metrics, and error reports.

Think of it as the **grading step** for the extraction pipeline: after
extraction runs and writes a production JSON, the eval module turns that JSON
into a lab-format XLSX, cross-references it against the SME's GT, and colors
every cell with a verdict the SME can scan in seconds.

## Quick start

Three modes, one CLI:

```bash
# 1. Single doc — GT lives in ground_truth/<doc>.xlsx
python -m evaluation --doc full_report_Redacted

# 2. Batch — auto-discover every doc that has BOTH a GT.xlsx AND
# an extraction_production.json
python -m evaluation --batch

# 3. Multi-sheet workbook — one workbook in, per-sheet outputs +
# a consolidated review workbook out
python -m evaluation --workbook ground_truth/gt_batch.xlsx
```

Output for each doc lands in:
- `local_runs/artifacts/<doc>/eval_*` — per-doc metrics + drill-down workbook
- `local_runs/evaluations/<input>_evaluated.xlsx` — consolidated colored review (workbook mode only)
- `local_runs/evaluations/<input>_errors.xlsx` — sheets that couldn't be resolved (workbook mode only)

## What the four verdict colors mean

Every cell in the review workbook gets one of four verdicts:

| Color | Verdict | Meaning |
|---|---|---|
| 🟢 Green (`#C6EFCE`) | **Match** | extracted value canonicalizes to the same string as the GT value |
| 🔴 Red (`#F8B4B4`) | **Mismatch** | both sides emitted a value, but they differ |
| 🟡 Yellow (`#FFE699`) | **Missed** | GT had a value, extraction emitted nothing |
| 🔵 Blue (`#BDD7EE`) | **Extra** | extraction emitted a value, GT had nothing |

A fifth color — 🟣 purple (`#D5B3E6`) — is defined but unused, reserved for a
future category (`NEEDS_REVIEW` / `UNCERTAIN` / whatever comes next) so the
palette can grow without needing to re-juggle existing hues.

The colors also drive the drill-down sheet tabs inside the per-doc
`eval_mismatches.xlsx`:
- **Review Results** — the SME-facing lab-layout sheet with per-cell colors
- **Mismatches** — flat list of every Red cell
- **Missed** — flat list of every Yellow cell
- **Extra** — flat list of every Blue cell

## The four output artifacts (per doc)

Whenever you run one of the three CLI modes, four files land per doc:

| File | Purpose |
|---|---|
| `eval_metrics.json` | Machine-readable numbers — precision, recall, F1, per-field, per-section, overall + the raw cell verdicts |
| `eval_report.txt` | Human-readable classification table |
| `eval_mismatches.xlsx` | 4-sheet SME workbook (Review Results + 3 drill-downs) |
| `eval_summary.md` | One-page Markdown summary |

The `eval_` prefix means these coexist safely with pipeline artifacts
(`extraction_production.json`, `link_metrics.json`, `agent_trace.json`, etc.)
in the same `local_runs/artifacts/<doc>/` folder — no filename collisions.

## The workbook flow — one file, one deliverable

The multi-sheet workbook mode (`--workbook`) is designed for SMEs who
have their GT split across many docs. Give it ONE workbook where each
sheet name identifies one source doc; get back ONE colored consolidated
review workbook where each tab is that doc's Review Results:

```
input                                     output
─────                                     ──────
gt_batch.xlsx                             local_runs/evaluations/
  ├── Sheet "demo"                          ├── gt_batch_evaluated.xlsx
  ├── Sheet "full_report_Redacted"          │      ├── Sheet "demo"
  ├── Sheet "patient_42.pdf"                │      ├── Sheet "full_report_Redacted"
  └── Sheet "INDEX"                         │      ├── Sheet "patient_42.pdf"
                                            │      └── (INDEX skipped)
                                            └── gt_batch_errors.xlsx
                                                   └── one row per skipped sheet
```

Sheet name → doc_id resolution uses **substring matching**: a sheet named
`06CDGMFM97SR` resolves to a folder called
`2025-12-09_..._06CDGMFM97SR_redacted` under `local_runs/artifacts/`. This
means the SME doesn't have to type the full artifact folder name to
reference a doc.

See [usage.md](usage.md) for the resolution rules (ambiguity, no-match,
extension stripping) and the error-report format.

## Reading order

1. **[architecture.md](architecture.md)** — the module's data flow, the
   matcher, the field-map contract, the identity resolution logic (why
   gene-only records like HLA-A / DPYD get their own row instead of
   collapsing).
2. **[usage.md](usage.md)** — every CLI flag, every default path, every
   output artifact, and end-to-end workflows for common SME scenarios.
3. **[field_map.md](field_map.md)** — how `evaluation/config/field_map.yaml`
   maps the 20-column lab layout to the extraction JSON paths, and how to
   add new columns.

## Related docs

- [architecture/schema.md](../architecture/schema.md) — the v4 schema that
  extraction produces (the input to eval)
- [reference/scripts.md](../reference/scripts.md) — operational scripts
  including the standalone `to_lab_xlsx` writer
- [PIPELINE_REFERENCE.md](../PIPELINE_REFERENCE.md) — the extractor pipeline
  whose output feeds into this eval module
