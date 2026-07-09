# Evaluation Module — Architecture

## Data flow at a glance

```
                  ┌───────────────────────────────────────────────────────┐
                  │             Extractor pipeline (upstream)              │
                  │  ─────────────────────────────────────────────────    │
                  │  PDF → preprocess → teams → linker → verifiers → …    │
                  │           │                                            │
                  │           ▼                                            │
                  │      local_runs/artifacts/<doc>/                       │
                  │      extraction_production.json                        │
                  └────────────┬──────────────────────────────────────────┘
                               │
              ┌────────────────┤
              ▼                ▼
    ground_truth/<doc>.xlsx    evaluation/config/field_map.yaml
    (SME-authored)             (column ↔ JSON path mapping)
              │                │
              └───────┬────────┘
                      ▼
   ┌────────────────────────────────────────────────────────────┐
   │                    evaluation/cli.py                        │
   │  ─────────────────────────────────────────────────────────  │
   │  1. resolve doc_id (substring match on artifacts folder)    │
   │  2. load_ground_truth  →  list[GTRow]                       │
   │  3. load_extraction    →  list[ExtractedRow]                │
   │  4. match_rows.match   →  list[RowPair]                     │
   │  5. metrics.compute    →  {metrics, _cell_verdicts}         │
   │  6. render_report.write_all  →  4 files per doc             │
   └───────────────────────────┬────────────────────────────────┘
                               │
                               ▼
              ┌────────────────────────────────┐
              │  local_runs/artifacts/<doc>/    │
              │  ─────────────────────────────  │
              │  eval_metrics.json              │
              │  eval_report.txt                │
              │  eval_mismatches.xlsx           │
              │  eval_summary.md                │
              └────────────────────────────────┘

  If `--workbook`:  also builds `local_runs/evaluations/<input>_evaluated.xlsx`
                    (consolidated colored review) + `_errors.xlsx` (skipped sheets)
```

## Module layout

```
evaluation/
├── __init__.py
├── __main__.py                    # so `python -m evaluation` works
├── cli.py                         # THE entry point (main, argparse, --doc/--batch/--workbook)
│
├── config/
│   └── field_map.yaml             # single source of truth for the 20-column lab layout
│
├── lib/                           # library code (no CLI)
│   ├── canonical.py               # cell-comparison canonicalizer (HGVS, HGNC, biomarker aliases)
│   ├── compare_cells.py           # per-cell verdict enum (TP/WRONG/FN/FP/TN) + compare()
│   ├── load_extraction.py         # extraction_production.json → list[ExtractedRow]
│   ├── load_ground_truth.py       # GT xlsx → list[GTRow]  (accepts optional sheet_name)
│   ├── match_rows.py              # pair up rows by identity (with gene-only fallback)
│   ├── metrics.py                 # verdict aggregation → P/R/F1
│   └── render_report.py           # writes the 4 output files + the consolidated xlsx
│
└── scripts/
    ├── bootstrap_gt.py            # generate a GT xlsx from inline seed content
    └── to_lab_xlsx.py             # STANDALONE: extraction_production.json → lab-layout xlsx (no GT)
```

## Key concepts

### `field_map.yaml` — the ONE source of truth

Every module that touches the 20-column lab layout — `load_ground_truth`,
`load_extraction`, `render_report`, `bootstrap_gt`, `to_lab_xlsx` — reads
column order and JSON-path mapping from
`evaluation/config/field_map.yaml`. Changing that YAML propagates to every
consumer without further edits. See [field_map.md](field_map.md).

### `RowPair` — the atom the whole flow revolves around

After matching, every row in either GT or extraction is represented as one
`RowPair`:

```python
@dataclass
class RowPair:
    section: str          # "variants" | "biomarkers"
    gt: GTRow | None      # None = extraction had this row, GT didn't
    extracted: ExtractedRow | None   # None = GT had this row, extraction missed it
    identity: tuple       # what determines "same entity" (see identity resolution below)
```

Three shapes:
- `gt != None, extracted != None` → **matched pair** → each cell gets a per-column verdict
- `gt != None, extracted == None` → **missed row** → every non-empty GT cell → Missed (FN)
- `gt == None, extracted != None` → **extra row** → every non-empty ex cell → Extra (FP)

### Identity resolution — how rows get paired

`match_rows.py` uses a compact identity tuple per row so it can pair GT
against extraction cheaply.

| Section | Full-detail identity | Gene-only fallback |
|---|---|---|
| variants | `(canonical(gene), canonical(amino_acid_change))` | `(canonical(gene),)` when AA change is absent |
| biomarkers | `(canonical(biomarker_name),)` | — |

The **gene-only fallback** is the fix for the vanishing-record bug: HLA
typing rows (`HLA-A`, `HLA-B`), pharmacogenomic rows (`DPYD`, `UGT1A1`,
`CYP2D6`, `TPMT`), and any other gene-keyed entities that don't carry HGVS
notation used to get a `None` identity, which collapsed them all onto a
single shared "(UNRESOLVED)" bucket in the renderer. Now each such row
gets a unique per-gene identity and renders as its own row.

### Canonicalization — why `p.G12D` matches `G12D`

`canonical()` in `lib/canonical.py` reuses existing pipeline infra:

- **HGNC resolution** — `HER2` → `ERBB2` via `preprocess.hgnc_resolver`
- **HGVS prefix strip** — `p.G12D` → `G12D` via `agents.linker._canon_change`
- **Biomarker synonyms** — `MSI` ↔ `MICROSATELLITE INSTABILITY` via
  `config/data/biomarker_synonyms.yaml`
- **VAF strict-numeric** — `"10%"` → `"10"`, but `"9.8"` ≠ `"10"`

No new alias YAMLs owned by the eval module — anything canonical comes from
the same tables the pipeline already trusts, so eval never disagrees with
extraction on identity.

### Doc-id resolution — substring matching

The `--workbook` and `--doc` modes both call `_resolve_doc_id(name, art_dir)`
which does case-insensitive substring matching against `local_runs/artifacts/*/`
subdirectory names. This means a sheet named `06CDGMFM97SR` resolves to
whatever folder contains that substring (e.g.
`2025-12-09_..._06CDGMFM97SR_redacted`).

Outcome cases:

| Result | Behavior |
|---|---|
| Exactly one folder matches AND has `extraction_production.json` | OK — run the eval on that folder |
| Multiple folders match | AMBIGUOUS — skip with error, record in `_errors.xlsx`, list all candidates |
| No folder matches | NO_MATCH — skip with warning, record in `_errors.xlsx` |
| Matched folder has no extraction | NO_MATCH — same handling as above |

Common extensions (`.pdf`, `.xlsx`, `.docx`) are stripped from the input name
before matching, so `HLA_report.pdf` searches for `HLA_report`.

## Verdict pipeline — from row pair to colored cell

```
    RowPair                                                     Colored cell
      │                                                              ▲
      │                                                              │
      ├─ if gt AND extracted:                                        │
      │      for each column:                                        │
      │          compare(gt_val, ex_val)  ─►  TP / WRONG / FN / FP   │
      │                                                              │
      ├─ if gt only:                                                 │
      │      for each non-empty gt cell: FN (Missed)                 │
      │                                                              │
      └─ if extracted only:                                          │
             for each non-empty ex cell: FP (Extra)                  │
                                        │                            │
                                        ▼                            │
                                 cell_verdicts (list of dicts)       │
                                        │                            │
                    ┌───────────────────┼────────────────────┐       │
                    ▼                   ▼                    ▼       │
          metrics.compute()   render_report.write_all()   returned  │
              │                          │              from _eval_one
              ▼                          ▼                          │
          eval_metrics.json       eval_mismatches.xlsx    ──────────┘
                                  (Review Results sheet
                                   colors each cell by
                                   its verdict)
```

## Interaction with the consolidated workbook (multi-sheet mode)

When `--workbook` is used, each per-doc eval ALSO appends its Review Results
sheet to a shared `openpyxl.Workbook` object accumulated across the whole
batch. After the loop, that shared workbook is saved as
`<input_stem>_evaluated.xlsx`. Same rendering function, same colors, same
hover comments — just one file for the SME to open instead of N.

Sheet name in the consolidated workbook = the ORIGINAL input sheet name
(preserved so the SME's mental sheet↔doc mapping survives). Truncated to 31
chars if longer (Excel limit).

## Error report (workbook mode)

If any sheet fails to resolve (NO_MATCH or AMBIGUOUS), the workbook run
also writes `local_runs/evaluations/<input_stem>_errors.xlsx`. One sheet
called `Errors`, red header, five columns:

| Column | Content |
|---|---|
| Sheet name | Original input sheet name |
| Attempted match key | The sheet name with common extensions stripped |
| Error type | `NO_MATCH` or `AMBIGUOUS` |
| Detail | Human-readable reason |
| Candidates | For AMBIGUOUS: comma-separated list of colliding folders |

Only written when there are errors — clean runs don't create a stray file.

## What happens INSIDE the per-doc `eval_mismatches.xlsx`

Four sheets, in this order:

1. **Review Results** — inserted at position 0. The colored lab-layout
   sheet. This is what the SME opens first.
2. **Mismatches** — flat list of every WRONG cell with GT + extraction
   side-by-side, both raw and canonical.
3. **Missed** — every FN cell (GT has a value, extraction doesn't).
4. **Extra** — every FP cell (extraction has a value, GT doesn't).

All 3 drill-down sheets share the same 9-column layout:
`doc_id | section | identity | column | verdict | gt | gt_canon | extracted | ex_canon`.
The raw + canonical pair lets you tell whether a Mismatch is a genuine
disagreement or just surface-form (`p.G12D` vs `G12D`).

## The consolidated workbook (`<input>_evaluated.xlsx`)

Only written by `--workbook` mode. One sheet per doc; sheet name matches
the original input sheet name. Each sheet is that doc's colored Review
Results — same layout, same colors, same hover comments as the per-doc
`eval_mismatches.xlsx` Review Results tab. **No** drill-down tabs in the
consolidated workbook — the SME goes to the per-doc file for those.
