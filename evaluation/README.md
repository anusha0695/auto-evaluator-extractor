# Evaluation

Compares `extraction_production.json` against an SME-authored ground-truth XLSX and produces:

- `metrics.json` — machine-readable precision / recall / F1 (per field, per section, overall)
- `classification_report.txt` — sklearn-style report
- `mismatches.xlsx` — 3 sheets (Wrong / Missing / Spurious), one row per non-matching cell
- `summary.md` — human-readable executive summary

## Usage

```bash
python -m evaluation --doc full_report_Redacted
```

Defaults (all relative to repo root):

```
Reads:
  ground_truth/<doc>.xlsx                              (SME-provided)
  local_runs/artifacts/<doc>/extraction_production.json

Writes:
  local_runs/evaluations/<doc>/metrics.json
  local_runs/evaluations/<doc>/classification_report.txt
  local_runs/evaluations/<doc>/mismatches.xlsx
  local_runs/evaluations/<doc>/summary.md
```

Override paths:

```bash
python -m evaluation \
  --doc full_report_Redacted \
  --gt ground_truth/full_report_Redacted.xlsx \
  --extraction local_runs/artifacts/full_report_Redacted/extraction_production.json \
  --out local_runs/evaluations/full_report_Redacted/
```

Batch all docs that have both inputs:

```bash
python -m evaluation --batch
```

## Comparison rules (the whole logic in 5 lines)

```python
if both empty:                       TN  (not counted in P/R)
if GT non-empty, extraction empty:   FN  (missing)
if GT empty, extraction non-empty:   FP  (spurious)
if canonical(GT) == canonical(EX):   TP  (match)
else:                                WRONG (real disagreement)
```

`canonical()` reuses **existing** pipeline infra:

- `agents.linker._canon_change` — strip HGVS prefix (`p.G12D` → `G12D`)
- `preprocess.hgnc_resolver.HGNCResolver.canonical` — gene aliases (`HER2` → `ERBB2`)
- `config/data/biomarker_synonyms.yaml` — biomarker aliases (`MSI` ↔ `MICROSATELLITE INSTABILITY`)

**VAF rule (strict-numeric):** values matching `/^\d+(\.\d+)?\s*%?$/` get
the `%` stripped, then strict string equality. So `10` == `10%` (unit
omission tolerated), but `9.8` ≠ `10` (precision drift surfaces).

**No new alias YAML in this module.** Adding aliases happens in the
existing `config/data/biomarker_synonyms.yaml` and HGNC table, not here.

## How row matching works (match-against-both)

The GT XLSX has no Section column. For each GT row:

1. Try as variant (identity = `(gene, amino_acid_change)`, canonicalized)
2. Try as biomarker (identity = `biomarker_name`, canonicalized)
3. Whichever matches an extracted row wins
4. If neither matches → reported as missing in best-guess section

Same canonicalization used for row keys as for cells, so SME shorthand
matches verbatim extraction.

## Files

```
evaluation/
├── README.md                       (this file)
├── __init__.py
├── __main__.py                     ← lets `python -m evaluation` work
├── cli.py
├── config/
│   └── field_map.yaml              ← only config: 20-column ↔ JSON paths
└── lib/
    ├── canonical.py                ← reuses existing infra; ~150 lines
    ├── compare_cells.py            ← the 5-line strict comparator
    ├── load_extraction.py          ← extraction_production.json → rows
    ├── load_ground_truth.py        ← <doc>.xlsx → rows
    ├── match_rows.py               ← match-against-both row pairing
    ├── metrics.py                  ← P/R/F1 at field/section/overall levels
    └── render_report.py            ← write all four output artifacts
```

## Dependencies

- `pyyaml` (already in repo)
- `openpyxl` — required to read GT XLSX and write `mismatches.xlsx`. If not
  installed, the module degrades: it writes `mismatches.tsv` instead.
  Install with `pip install openpyxl`.
