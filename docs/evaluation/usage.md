# Evaluation Module — Usage

## Command reference

The eval CLI is invoked as `python -m evaluation`. Three modes; pick the one
that matches what you have.

### `--doc <id>` — evaluate one document

```bash
python -m evaluation --doc full_report_Redacted
```

Inputs it looks for:

| Input | Default path |
|---|---|
| Ground truth | `ground_truth/<doc>.xlsx` |
| Extraction | `local_runs/artifacts/<doc>/extraction_production.json` |

If the `<id>` doesn't exactly match a folder under `local_runs/artifacts/`,
it's treated as a substring. So `--doc 06CDGMFM97SR` will resolve to
`local_runs/artifacts/2025-12-09_..._06CDGMFM97SR_redacted/` if that folder
exists.

Overrides:

```bash
python -m evaluation --doc full_report_Redacted \
    --gt /path/to/gt.xlsx \
    --extraction /path/to/extraction.json \
    --out /path/to/output/dir
```

When `--extraction` or `--out` are given, substring resolution is skipped
(the user is pointing at exact paths). Outputs land in
`local_runs/evaluations/<doc>/` unless `--out` is specified.

### `--batch` — evaluate every doc that has both a GT and an extraction

```bash
python -m evaluation --batch
```

Auto-discovers the intersection of:
- Every `.xlsx` file in `ground_truth/`
- Every `local_runs/artifacts/<name>/extraction_production.json`

Runs each doc through the same flow as `--doc`. Non-zero return code on any
failure; individual failures don't stop the batch.

### `--workbook <path>` — evaluate a multi-sheet GT workbook

```bash
python -m evaluation --workbook ground_truth/gt_batch.xlsx
```

For each sheet in the workbook:
1. Treat the sheet name as a substring search key
2. Find the matching folder under `local_runs/artifacts/`
3. Run the eval (same code path as `--doc`)
4. Write per-doc outputs to that folder with `eval_` prefix
5. ALSO append the doc's Review Results to a consolidated workbook

After the loop:
- `local_runs/evaluations/<input_stem>_evaluated.xlsx` — the consolidated review
- `local_runs/evaluations/<input_stem>_errors.xlsx` — sheets that couldn't be resolved (only if any)

### `-v` / `--verbose`

Adds debug-level logs. Recommended when troubleshooting resolution failures.

## Input formats

### Ground-truth XLSX

The GT workbook uses the 20-column lab layout — same columns as the review
workbooks. Header row is column-name only; data starts row 2. The eval
loader reads the first sheet by default, OR a specific sheet if
`sheet_name=` is passed (which the `--workbook` mode does automatically).

Column list (in field_map order):

```
Biomarker | Method | Result | Interpretation | Reference Range |
Variant Allele Freq | DNA Change Type | Amino Acid Change Type |
Genomic DNA Change | Genomic Ref Seq | Coding DNA Change |
Transcript Ref | Amino Acid Change | AA Ref Seq |
Clinical Significance | Genomic Source | Ref Assembly |
Chromosome | Genomic Position | Exon
```

**Bootstrapping a GT from seed content:**

```bash
# The bootstrap script has inline content for known docs; extend it if you
# want to seed a new doc.
python evaluation/scripts/bootstrap_gt.py --doc full_report_Redacted
# → ground_truth/full_report_Redacted.xlsx
```

For docs whose GT lives in a multi-sheet workbook, you don't need
bootstrap_gt — you just build the workbook directly with one sheet per doc.

### Extraction JSON

`extraction_production.json` is the output of `transform/to_production.py`
(part of the extractor pipeline). It follows the v4 schema, with
`Genomic_Variant_umbrella.Genomic_Variants[]` and
`other_molecular_biomarker_umbrella.other_molecular_biomarkers[]` as the
two arrays the eval module reads.

Nothing new to build here — this is the standard extractor output. Just
make sure the pipeline has run for the doc before running eval.

## Common workflows

### 1. New doc — start-to-finish eval

```bash
# 1. Run the extractor on the source PDF (assumes make target is defined)
make run-local PDF=/path/to/report.pdf PHASE=3

# 2. Bootstrap or hand-write the GT
python evaluation/scripts/bootstrap_gt.py --doc report

# 3. Evaluate
python -m evaluation --doc report

# 4. Open the drill-down workbook
open local_runs/artifacts/report/eval_mismatches.xlsx
```

### 2. SME reviewing a batch — one command, one file to open

```bash
# SME hands you gt_batch.xlsx with one sheet per doc they reviewed
python -m evaluation --workbook ground_truth/gt_batch.xlsx

# Open the consolidated colored review — flip between sheet tabs to check each doc
open local_runs/evaluations/gt_batch_evaluated.xlsx
```

If any sheets were skipped, also open `gt_batch_errors.xlsx` to see why.

### 3. Regression tracking — rerun eval after a code change

```bash
# after a prompt / matcher change, rerun the extractor
make run-local PDF=... PHASE=3

# rerun eval and diff the metrics
python -m evaluation --doc report
diff prior_run/eval_metrics.json local_runs/artifacts/report/eval_metrics.json
```

### 4. Standalone lab-format workbook (no GT, no verdicts)

If you just want to see the extraction rendered in the lab format — for
example, to hand to a clinician for FRESH review that becomes the GT —
use the standalone script:

```bash
python -m evaluation.scripts.to_lab_xlsx --doc full_report_Redacted
# → local_runs/artifacts/full_report_Redacted/review.xlsx
```

This does NOT do GT comparison. No colors, no [MISSED] tags, no drill-down
sheets — just the data laid out in the 20-column format for a human to
read.

## Output artifacts — what each file is

### Per doc (in `local_runs/artifacts/<doc>/`)

| File | Purpose | Format |
|---|---|---|
| `eval_metrics.json` | Precision/recall/F1 per field/section/overall + raw cell verdicts | JSON |
| `eval_report.txt` | Printable classification table | plain text |
| `eval_mismatches.xlsx` | 4-sheet SME workbook | XLSX |
| `eval_summary.md` | One-page summary of the run | Markdown |

Inside `eval_mismatches.xlsx`:

| Sheet | Content |
|---|---|
| Review Results | Lab-layout, one row per variant/biomarker, each cell colored by verdict |
| Mismatches | Flat list of every Red cell |
| Missed | Flat list of every Yellow cell |
| Extra | Flat list of every Blue cell |

### Consolidated / batch (in `local_runs/evaluations/`)

| File | When it's written | Purpose |
|---|---|---|
| `<input>_evaluated.xlsx` | `--workbook` mode | One sheet per doc — colored Review Results |
| `<input>_errors.xlsx` | `--workbook` mode, if any resolution errors | List of skipped sheets with reasons |

For `--doc` and `--batch` modes, files land in
`local_runs/evaluations/<doc>/` without prefix (`metrics.json`,
`classification_report.txt`, `mismatches.xlsx`, `summary.md`).

## Reading the colors

Quick legend (also in [README.md](README.md)):

- 🟢 **Green — Match**: extracted matches GT (after canonicalization)
- 🔴 **Red — Mismatch**: both sides emitted a value, they differ. Cell shows the extraction value; hover comment shows both.
- 🟡 **Yellow — Missed**: GT had a value, extraction emitted nothing. Cell shows `[MISSED] <gt value>`.
- 🔵 **Blue — Extra**: extraction emitted a value, GT had nothing. Cell shows the extraction value.

Every colored cell has a **hover comment** describing the verdict:
- Red: `MISMATCH\nGT: <x>\nExtracted: <y>`
- Yellow: `MISSED\nGT had: <x>`
- Blue: `EXTRA\nExtraction had: <x>\n(GT had no value here)`

## Interpreting the metrics.json

Top-level fields:

```json
{
  "doc_id": "full_report_Redacted",
  "generated_at": "2026-06-19T...",
  "ground_truth_file": "...",
  "extraction_file": "...",
  "row_level":   { "variants": {...}, "biomarkers": {...}, "overall": {...} },
  "field_level": { "Biomarker": {...}, "Result": {...}, ... },
  "overall":     { "tp": 71, "fp": 19, "fn": 3, "wrong": 9,
                   "precision": 0.72, "recall": 0.85, "f1": 0.78 }
}
```

- **Row-level**: matched vs missed vs extra (i.e. record-level recall/precision — one number per row, not per cell)
- **Field-level**: for each column (Biomarker, Method, Result, …), how often that column agreed / disagreed / was Missed / was Extra
- **Overall**: aggregate across every cell of every row

A WRONG cell (Red) is counted as BOTH an FP and an FN at field level (the
extractor said something AND missed the truth at the same coordinate).
This matches the sklearn convention for multi-class classification without
overlapping classes.

## Troubleshooting

### "Sheet name → NO_MATCH"

The sheet name (case-insensitive, after stripping `.pdf`/`.xlsx`/`.docx`)
didn't appear as a substring in any folder under `local_runs/artifacts/`.
Check:

```bash
ls local_runs/artifacts/
```

Then either rename your sheet to include a substring of the actual folder
name, or use a more specific identifier.

### "Sheet name → AMBIGUOUS"

The sheet name substring matched multiple folders. The error report will
list them. Rename the sheet to something more specific (add more of the
folder name, remove ambiguous prefixes).

### "workbook not found"

Check the `--workbook` path is correct and readable. Absolute paths are
safest.

### "consolidated workbook NOT written"

Every sheet failed to resolve (all NO_MATCH or all AMBIGUOUS). Open the
`_errors.xlsx` file to see which sheets failed and why.

### Cells I expect to be green are yellow, or vice-versa

Cell display shows the EXTRACTED value on green/red/blue cells but the
GT value on yellow cells. If a green cell shows a different string than
you expect, look at the hover comment — the canonicalizer may have
normalized `p.G12D` to `G12D` (identical for identity purposes but not
visually).

If a cell is unexpectedly Missed / Extra, check `eval_metrics.json` for
the exact `gt_canon` and `ex_canon` values that the comparator used —
that shows you what canonicalization was applied on both sides.
