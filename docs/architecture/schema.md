# Schema, Data Shapes, and Production Mapping

The internal schema is `config/schemas/genomic_pathology_v3.json` (Pydantic models are
generated from it at runtime by `core/schema_loader.py` — change the JSON, no Python
changes needed). `genomic_pathology_v2.json` is retained only for the frozen graph_v1 /
metadata baseline.

---

## 1. Top-level sections (v3)

| Section | Owning team | Shape |
|---|---|---|
| `report_metadata` | metadata_team | flat object (patient/report/provider fields) |
| `other_molecular_biomarker_umbrella` | molecular_biomarker_team | array of biomarker objects |
| `tested_biomarker_umbrella` | tested_biomarker_team | the panel — what was tested (incl. no-result) |
| `significant_findings` | specimen_findings_team | array of specimen-finding records |
| `clinical_information` | clinical_info_team | object (history / diagnosis / context) |
| `count_of_extracted_objects` | (bookkeeping) | counts; not an extracted entity |

## 2. The biomarker two-level model

This is the most important shape. **One biomarker section** holds every biomarker the
report gives a result for — protein-expression markers (HER2, ER, PR, Ki-67, PD-L1),
aggregate molecular scores (TMB, MSI, HRD), **and gene sequence variants** (JAK2 V617F,
BRAF V600E, fusions). Variants are *not* a separate section — they are biomarker findings
whose `result` is the call and whose genomic specifics live in a nested `variant_detail`.

```
biomarker (e.g. HER2)                         one object per biomarker identity
  ├─ biomarker_class                          protein_expression | molecular_score | sequence_variant
  └─ findings[]                               ONE entry per distinct test/method
       ├─ {method: IHC,  result: "2+", interpretation: "Equivocal", ...}
       ├─ {method: FISH, result: "Amplified", ...}
       │    └─ occurrences[]                  ONE entry per RESTATEMENT (verbatim surface kept)
       └─ variant_detail                      populated ONLY for sequence_variant findings
```

- **`findings` length = number of distinct tests.** `HER2 IHC 2+` and `HER2 FISH
  amplified` are the *same* biomarker, *two* findings.
- **`occurrences` length = number of times that result was stated** (results table +
  synoptic summary, possibly worded differently). Verbatim surfaces are never overwritten;
  an amended/addendum value wins the canonical slot and the original is kept with
  `superseded: true`.
- **`biomarker_class`** tags each biomarker so consumers can filter without re-deriving.
  `sequence_variant` biomarkers MUST carry a populated `variant_detail`; the other two
  classes leave it `null`.

### `variant_detail` (sequence variants only)

Verbatim HGVS exactly as printed (`coding_dna_change`, `genomic_dna_change`,
`amino_acid_change`, transcript accessions) **plus** a separately-stored canonical form
from `hgvs_validate` in `hgvs_normalized`. `gene_symbol` is HGNC-preferred. VAF, genomic
source class, clinical significance, assembly, exon, etc. are all verbatim, `null` when
not on the page — never inferred.

## 3. The `provenance` contract (per-field "why")

Every section carries a `provenance` value typed `["array","object","null"]` — the
**preferred** shape is an **array** of `{field_name, block_id, page, type, rationale}`,
one entry per populated field; a legacy keyed map is still accepted for back-compat; and
`null` is valid so nothing fails validation. `type` ∈ `verbatim | inferred | derived |
absent`; `rationale` is one short clause (required for `inferred`/`derived`).

This is the per-field reasoning the SME reviewer reads and what powers the UI's
"↳ why (this field)" line. **All five provenance-bearing sections** (report_metadata,
biomarker record, finding, specimen record, clinical_information) emit it as an array —
the team prompts instruct "ALWAYS emit, as an ARRAY". On the read side, `ui/app.js`
fetches `extraction_v2.json` and iterates each record's `provenance` array to populate
the field-detail panel; the scorer reads the same shape server-side. Both accept the
legacy keyed map for back-compat. The scorer excludes `provenance`, and ground truth
carries none, so it has zero accuracy impact.

## 4. Provenance vs occurrences

Two distinct provenance mechanisms, both required:

- **`occurrences[]`** (on a grouped finding) — one entry per restatement of the value,
  each `{block_id, page, char_start, char_end, surface, superseded}`. This is the
  *value-level* evidence: where the result text physically appears.
- **`provenance[]`** (per record/finding) — one entry per *field*, the "why I chose this
  value" with a `type`. This is the *field-level* reasoning.

## 5. Significant findings (specimen)

`significant_findings.specimen_findings[]` — one record per specimen. Each holds the
specimen(s) (tissue type, laterality), staging (`pTNM_staging_details`), nodal status,
gross/microscopic descriptions, histologic type, and the final diagnosis. Attribution
matters here: when a record has multiple `specimen[]` entries, the AttributionVerifier
checks that an attribute (e.g. `tissue_type`) is anchored to the correct owner via the
owner key (`specimen_id`), falling back to position when the key is null.

## 6. Production mapping (`transform/to_production.py`)

The internal envelope is converted to the **production schema** for delivery:

- **`admin_inverse`** + `config/production_mapping.yaml` — the field-name mapping from
  internal refs to production locations.
- **`production_label(ref)`** — the production display location for an internal ref;
  returns `None` for internal-only refs (those are dropped from the production view).
  Several `variant_detail.*` subfields fold into one "details (folded)" production label
  while keeping their distinct internal refs (so each still resolves its own provenance).
- The conversion **reshapes** (nested → production layout), **folds** (variant_detail →
  details), and **filters** (drops internal-only fields). On committed graph_selfcorrecting runs the
  production output is auto-emitted as `extraction_production.json` and is what the
  Production browser in the UI renders.

See `PRODUCTION_MAPPING` content (now under `docs/` history) for the full field list, and
[reference/traceability.md](../reference/traceability.md) for the gate that locks the
conversion (`gate_p3_m9_production_conversion`).

`to_production` is **shape-tolerant**: it accepts the v3 nested `findings[]` AND the v4
flat biomarker record, and folds the v4 separate `Genomic_Variant_umbrella` into the same
`pathology_biomarkers_findings` section — so the production contract is identical on either
internal shape (`gate_v4_m8_production`).

---

## 7. v4 — mCODE `genomic_pathology_extraction` (current target)

A separate, **non-destructive** internal schema `config/schemas/genomic_pathology_v4.json`
(bound by `config/teams_v4.yaml`; selected by `--version v4` / `make run-local PHASE=4`).
v3 and all its gates stay green; v4 adds new files alongside. Differences from v3:

| Change | v3 | v4 |
|---|---|---|
| Gene sequence variants | a biomarker `finding` with nested `variant_detail` | their **own** `Genomic_Variant_umbrella` (revived `genomic_variant_team`) — one flat record per variant, with verbatim HGVS + HGNC gene canonicalization |
| `other_molecular_biomarker_umbrella` | nested `findings[]` + `variant_detail` | **FLAT** — one record per non-variant biomarker (protein expression / molecular scores), 6 fields |
| `significant_findings`, `clinical_information` | active teams | **DISABLED by default** via `enabled: false` (Decision D3, `core/section_toggle.py`) — code/prompt/schema kept; flip to revive |
| Verifier floor | normalization (canonical-diff) | **+ HGVS structural-validity** (`verification/hgvs_validity.py`) — malformed HGVS → `needs_review` (never renormalize) |

The four active v4 sections are `report_metadata`, `Genomic_Variant_umbrella`,
`other_molecular_biomarker_umbrella` (flat), `tested_biomarker_umbrella`. Disabled sections
are skipped by the planner / graph team-set / verifier suite / scorer / schema validator
(all consult `core/section_toggle.py`). Full migration log + the 6 locked decisions:
[`docs/migration/genomic_pathology_v4_plan.md`](../migration/genomic_pathology_v4_plan.md);
the v4 gates are in [reference/traceability.md](../reference/traceability.md).
