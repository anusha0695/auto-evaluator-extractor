# Evaluation Module — `field_map.yaml` Contract

`evaluation/config/field_map.yaml` is the **single source of truth** for
the eval module's column layout. Every consumer reads it:

- `load_ground_truth.py` — matches workbook column headers to expected fields
- `load_extraction.py` — maps each JSON record's fields into cells keyed by column header
- `render_report.py` — writes the Review Results sheet in the same column order
- `bootstrap_gt.py` — writes GT xlsx templates in the same column order
- `scripts/to_lab_xlsx.py` — writes the standalone review xlsx in the same column order

If you want to add / rename / re-order a column, this is the ONE file to edit.

## File shape

```yaml
sections:
  - variants        # first: Genomic_Variant_umbrella records
  - biomarkers      # second: other_molecular_biomarker_umbrella records

columns:
  - header: "Biomarker"
    sources: { variants: gene_studied,        biomarkers: biomarker_name }

  - header: "Method"
    sources: { variants: method,              biomarkers: method }

  # ...20 columns total (see full file for the current layout)

identity_keys:
  variants:
    - gene_key
    - amino_acid_change
  biomarkers:
    - biomarker_name
```

## The column entry

```yaml
- header: "Some Column"
  sources:
    variants: <JSON field name on Genomic_Variants[]>
    biomarkers: <JSON field name on other_molecular_biomarkers[]>
```

- `header` — displayed in the XLSX header row. String.
- `sources` — maps each section to the JSON field name that populates this column for THAT section.
  - Use `""` (empty string) to leave the column blank for a section that doesn't have this concept
    (e.g. `Variant Allele Freq` is a variant-only concept, so `biomarkers: ""`).
  - Field name must match the extraction JSON key exactly.

## The two amino-acid columns

The lab layout has two distinct amino-acid columns:

| Position | Header | Meaning | JSON path (variants) |
|---|---|---|---|
| 8 | `Amino Acid Change Type` | Category of change (Substitution, Deletion, ...) | `amino_acid_change_type` |
| 13 | `Amino Acid Change` | HGVS protein notation (`p.G12D`, `V617F`) | `amino_acid_change` |

Do NOT collapse these — they carry different values. Earlier versions of
this file had both headers as `"Amino Acid Change"` (a legacy from the
reference workbook where the second header was implicitly the type
column). The eval module now treats them as distinct, and both are read
from separate JSON keys.

## `identity_keys` — how row matching works

The `identity_keys` block tells the row matcher what fields identify a
row uniquely PER SECTION:

```yaml
identity_keys:
  variants:
    - gene_key                 # canonical form of gene_studied (HGNC-resolved)
    - amino_acid_change        # HGVS protein change
  biomarkers:
    - biomarker_name           # canonical form of biomarker_name
```

For variants, the matcher builds `(gene, amino_acid_change)` as the
identity. When amino_acid_change is missing (e.g. HLA typing, DPYD
pharmacogenomic entries), it falls back to `(gene,)` only — this is the
fix for the vanishing-record bug. See
[architecture.md](architecture.md#identity-resolution).

## Adding a new column

Suppose the schema gains a `nomenclature_source` field on variants and you
want to review it in the workbook. Two steps:

1. Edit `field_map.yaml`, add a new column entry after the field you want
   it beside:

    ```yaml
    - header: "Nomenclature Source"
      sources: { variants: nomenclature_source, biomarkers: "" }
    ```

2. That's it. Every consumer picks it up automatically:
   - GT xlsx will have a `Nomenclature Source` column
   - Extraction rendering will pull `variants[].nomenclature_source` into that column
   - Review Results sheet will color the cell by verdict
   - Metrics will report per-field P/R/F1 including this new column

**No** code changes anywhere in `evaluation/`.

## Renaming a column

Change the `header:` value. The consumers all use `header` as the dict
key so this cascades cleanly. BUT — existing GT workbooks will still have
the OLD header text on their sheets, so:

- Regenerate GT files: `python evaluation/scripts/bootstrap_gt.py --doc <id>` for docs whose GT is scripted
- Manually update GT xlsx files whose content is hand-authored, using find/replace on the header row

## Removing a column

Delete the entry from `columns:`. The column disappears from every
downstream output. If existing GT workbooks still have the removed
column, its cells are silently ignored at load time.

## Column order

The order of entries under `columns:` is the order they appear in the
workbook. Change order by rearranging the YAML — no other edits needed.
The header row and every data row will re-flow accordingly.

## What `sections:` does

Just declares the two logical "buckets" the loader supports. Right now
it's fixed at `variants` and `biomarkers` because those are the two JSON
arrays the extractor produces. If a new umbrella comes online (e.g.
`tested_biomarker_umbrella` was added in v4), you'd add:

1. A section here: `sections: [variants, biomarkers, tested]`
2. Each column's `sources:` gains a `tested:` key
3. `load_extraction.py` gains a load path for that section
4. `match_rows.py` gains an identity-tuple builder for that section

## Duplicate-header defense

If a future edit reintroduces a duplicate header (e.g. two entries both
called `"Amino Acid Change"`), the loaders now handle it defensively:

- `load_ground_truth.py` uses **first-occurrence** for the column-index lookup
- `load_extraction.py._cells_from_record` also uses first-occurrence
- `metrics.py` de-dupes headers before iteration

This means a duplicate wouldn't crash, but the second occurrence would be
ignored. Comments in each file spell this out for future maintainers.

The recommended state is: unique headers only. The defensive fallback is
just for surviving accidents while you fix them.

## Related files

- `evaluation/config/field_map.yaml` — the file itself
- `evaluation/lib/load_ground_truth.py` — reader for GT xlsx (uses this config)
- `evaluation/lib/load_extraction.py` — reader for extraction JSON (uses this config)
- `evaluation/lib/render_report.py` — writer for review xlsx (uses this config)
- `evaluation/scripts/bootstrap_gt.py` — GT xlsx bootstrapper (uses this config)
- `evaluation/scripts/to_lab_xlsx.py` — standalone lab-xlsx writer (uses this config)
