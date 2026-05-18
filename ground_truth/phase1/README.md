# Phase 1 Ground Truth Validation Set

Manually-extracted ground truth JSON files used to score the Phase 1 pipeline output.

- **Primary doc:** `demo.pdf` (real NeoGenomics fax for William Dawson, JAK2 V617F Mutation Analysis — Quantitative). Ground truth lives one level up at [`../demo.json`](../demo.json) and covers **all four umbrella sections** of schema v2.
- **Synthetic fixtures (this folder):** 5 mock PDFs from two distinct templates. Ground truth covers **only the `report_metadata` section**. The other three umbrellas (`Genomic_Variant_umbrella`, `other_molecular_biomarker_umbrella`, `tested_biomarker_umbrella`) are emitted as empty placeholders to satisfy the schema-v2 always-emit-all-4-sections rule and will be hand-labelled in Phase 2/3.

Schema: `config/schemas/genomic_pathology_v2.json`.

## Phase 1 acceptance criterion

Pipeline correctness **≥ 80% per-field match against `demo.json` `report_metadata`** is the Phase 1 acceptance gate.
The 5 synthetic fixtures in this folder are secondary regression coverage — they should also exceed 80% on `report_metadata`, but `demo.pdf` is the primary criterion because it is a real, in-distribution document.

## Inventory

### Primary (one level up)

| Doc ID | Source PDF | Format | Pages | Scope of ground truth |
|---|---|---|---|---|
| demo | data/actual_docs/demo.pdf | NeoGenomics JAK2 V617F Quantitative (real fax) | 2 | **All 4 umbrella sections** |

### Synthetic fixtures (this folder)

| Doc ID | Source PDF | Format | Pages | Scope of ground truth |
|---|---|---|---|---|
| doc_3   | doc_3.pdf   | Tempus xT                    | 3 | `report_metadata` only |
| doc_11  | doc_11.pdf  | Tempus xT                    | 3 | `report_metadata` only |
| doc2_1  | doc2_1.pdf  | unknown_vendor_v2 (doc2 tmpl) | 5 | `report_metadata` only |
| doc2_6  | doc2_6.pdf  | unknown_vendor_v2 (doc2 tmpl) | 5 | `report_metadata` only |
| doc2_25 | doc2_25.pdf | unknown_vendor_v2 (doc2 tmpl) | 5 | `report_metadata` only |

## Key findings from manual inspection — these drive prompt design

### demo.pdf — real-world quirks

1. **Faxed report** — page 1 (and to a lesser extent page 2) carries a fax-transport header band ("** INBOUND NOTIFICATION : FAX RECEIVED SUCCESSFULLY **" / "TIME RECEIVED ... REMOTE CSID NeoGenomics ... DURATION 91 PAGES 2 STATUS Received" / "08.27.2025 01:41:25 Page: 1 of 2 Sender: NeoGenomics Laboratories, Recipient: +19512529699"). This is **not** part of the report; the extractor must filter it out or the model will hallucinate dates from the fax timestamp.
2. **Vendor (`NeoGenomics`) is verbatim** in the masthead logo, the page-1 phone block, the page-1 copyright footer, and the page-2 lab address. No inference needed.
3. **Practice ≠ vendor** — "Client 2641 — Hematology & Oncology Consultants" at 28078 Baxter Rd. Suite 140, Murrieta, CA 92563 is the **ordering practice**, not NeoGenomics. The critical disambiguation rule.
4. **MRN field is literally blank** (`MRN:` with no value). Per the strict rule, `Patient_MRN = null`. The Accession / CaseNo `7220225 / MOL25-157872` is **not** an MRN.
5. **Dates need normalization** — printed as `MM/DD/YYYY HH:MM:SS PM PDT`, output as ISO `YYYY-MM-DD` (date only).
6. **Negative result** — JAK2 V617F (1849G>T) "Not Detected". Schema-v2 still emits a Genomic_Variant entry with `result: "Not Detected"` and `VAF: "0%"`, because the report explicitly tests-and-reports this variant. Tested_biomarkers is `["JAK2"]`.

### The 16 synthetic PDFs in the dev bucket are TWO distinct templates

**`doc_*` batch (Tempus xT format)** — 2 files in the Phase 1 set (doc_3, doc_11):

- Single dense page-1 with patient + diagnosis + accession + Physician + Institution in left margin.
- 3 pages total.
- "xT" product marker in the upper-right of page 1 identifies the lab vendor as Tempus.
- Has explicit `Physician` and `Institution` fields (clinical referring info).
- Has a "signing pathologist" with CLIA number at the bottom of each page (lab side). Signing pathologist is **not** a schema-v2 field.
- Recurring oddity: physician and signing pathologist often share the same name (mock-data quirk).
- ID # is the same as Accession Number — no separate MRN. (Schema v2 has no Accession_Number field in report_metadata.)

**`doc2_*` batch (unknown vendor)** — 3 files in the Phase 1 set (doc2_1, doc2_6, doc2_25):

- Multi-page (5 pages) with Patient/Specimen/`Ordered By` columns on page 1.
- `Ordered By` column header has no value beneath it — physician must be extracted from the page footer (every page footer reads `PATIENT: <name>  CASE NUMBER: <#>  PHYSICIAN: Dr. <name>`).
- No vendor branding anywhere in the report — `Vendor_Name = null`.
- No practice / institution name visible.
- No signing pathologist.
- Two different diagnoses present: header diagnosis (e.g. "Triple-negative breast cancer") and page 4 pathological diagnosis (recycled wording across the batch — looks templated). Diagnoses are not in `report_metadata`.
- Case Number serves as the accession identifier; no separate MRN.

### Prompt-design implications

1. **Fax-header filtering** — for documents that arrived by fax (`demo.pdf` is the canonical example), strip the leading fax-status band before extraction; otherwise the model picks up the fax timestamp (`08.27.2025 01:41:25`) and confuses it with `Report_Date`.

2. **Vendor inference rule** — when an explicit vendor name isn't present, look for product markers ("xT" → Tempus, "FoundationOne" → Foundation Medicine, "xGEN" → Caris, etc.). If no vendor marker found, set `Vendor_Name = null`.

3. **Practice vs vendor disambiguation** — in `doc_*` reports, "Institution" is the ordering physician's clinic ("Doyle PLC", "Gutierrez Group") not the testing vendor. In `demo.pdf`, "Client 2641 — Hematology & Oncology Consultants" is the practice, **not** NeoGenomics. In `doc2_*` reports, practice is simply not present.

4. **Physician location varies by template** — in `doc_*` reports it's in the left margin under "Physician". In `doc2_*` reports it's only in the page footer. In `demo.pdf` it's the "Ordering Physician(s):" line in the right column of the header. Extractor needs to check all three locations.

5. **Title parsing** — `demo.pdf` prints "STANLEY D SCHINKE, M.D." (all caps, comma-prefixed M.D., embedded middle initial). The extractor must (a) preserve title casing for first/last name (`Stanley` / `Schinke`), (b) drop the middle initial (no schema field), (c) capture title as `"M.D."`.

6. **MRN handling** — neither template provides a true MRN in this corpus. The "ID #" / "Case Number" / "MRN: <blank>" fields are accession identifiers or empty fields, not patient MRNs. `Patient_MRN = null` is the expected output for every report in this set.

7. **Date conventions** — synthetic docs use ISO `YYYY-MM-DD` already (no conversion). `demo.pdf` uses US `MM/DD/YYYY HH:MM:SS PM PDT` — must be normalized to ISO date.

8. **Always-emit-all-4-sections rule** — even when an umbrella has nothing in it, emit it with `count: 0`, `llm_confidence_score: null`, and an empty array. The 5 synthetic ground truth files in this folder use this convention.

9. **Coverage-Auditor priority gaps** — most likely missed entities in this corpus:
   - Vendor name (often inferred, not stated).
   - Practice info in `doc_*` (small text in left margin, easy to miss).
   - Physician in `doc2_*` (only in footer, not header).
   - In `demo.pdf`: the multi-line practice address (street, suite, city/state/zip on separate lines under the practice name).

## File format

Each ground truth file follows this shape (matches schema `config/schemas/genomic_pathology_v2.json`):

```jsonc
{
  "doc_id": "...",
  "source_pdf": "...",
  "report_format": "Tempus_xT" | "unknown_vendor_v2" | "NeoGenomics_JAK2_v1",
  "page_count": N,
  "schema_version": "genomic_pathology_v2",
  "phase1_scoring_scope": "report_metadata only" | "all_sections",
  "ground_truth_extracted_by": "manual_review",
  "ground_truth_notes": ["..."],
  "genomic_pathology_extraction": {
    "count_of_extracted_objects": N,
    "report_metadata":                       { ... },
    "Genomic_Variant_umbrella":              { "count_of_Genomic_Variants": N, "llm_confidence_score": null, "Genomic_Variants": [...] },
    "other_molecular_biomarker_umbrella":    { "count": N, "llm_confidence_score": null, "other_molecular_biomarkers": [...] },
    "tested_biomarker_umbrella":             { "count_of_tested_biomarkers": N, "page_numbers": [...], "llm_confidence_score": null, "tested_biomarkers": [...] }
  }
}
```

At validation time the pipeline output's `genomic_pathology_extraction.report_metadata` is matched field-by-field against this file's `genomic_pathology_extraction.report_metadata`. For `demo.json` only, all four umbrellas are also matched.
