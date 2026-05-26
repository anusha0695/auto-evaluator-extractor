# BACKLOG — deferred work (revisit later)

Living list of intentionally-deferred items. When Sri says "add to todo", add it
here with enough detail that we can pick it up cold after a long gap. Keep this
file up to date as items land or change.

---

## B1 — Per-field extractor rationale for EVERY field

**Status:** ✅ DONE (data + UI side). Schema now declares an optional `provenance`
map on the biomarker record, each finding, each specimen_findings record, and
clinical_information (same shape as report_metadata's); the
molecular_biomarker / specimen_findings / clinical_info team prompts now instruct
the model to emit it with a per-field `rationale`. The UI was already generic
(`evidence.field_rationale_map` reads any provenance map), so the Entity browser +
SME trace surface these per-field reasons with no UI change. Gates green
(verify_phase2_m1 section shapes + all prompts render; verify_phase3_m8*).
**Requires a fresh v3 run to populate** — existing cached runs predate the new
provenance fields, so biomarker/specimen/clinical per-field reasons appear only
after re-running: `make run-local PDF=./data/actual_docs/demo.pdf PHASE=3`.
tested_biomarkers stays a plain string list (no per-field rationale — nothing to
attach). report_metadata already had this since Phase 2a.

**Original framing (kept for history):** We first implemented option 2(a) (surface the per-field
reasons we ALREADY store: report-metadata `provenance[field].rationale`,
CoverageAuditor `missed_fields[].why_extractor_should_have_caught_it` /
`spurious_fields[].evidence_against`, Arbiter `re_extract_hints[].hint`, and VMAW
`rationale`; section-level reasoning elsewhere). The SME trace shows a real
per-field "why" wherever one exists.

**The gap B1 closes:** biomarker findings (and other non-metadata records) do NOT
store a per-field rationale today — `finding.rationale` does not exist. So when an
SME selects e.g. `result: Not Detected`, the *extraction* step has no field-level
"why" from the extractor itself (only the section-level extractor thought + any
auditor/arbiter/VMAW reasons that happened to single it out). B1 makes the
Extractor emit a short, per-field reason for every value it writes.

**Why it's a bigger change (and why we deferred it):**
- It changes the **extractor contract**: the model must return a reason per field.
- It touches the **schema** (`config/schemas/genomic_pathology_v3.json`): each
  array record / field needs a place to hold `extraction_rationale` (mirroring how
  `report_metadata.provenance[field].rationale` already works — ideally reuse that
  exact `provenance` pattern for the umbrella sections so the UI path is uniform).
- It touches the **team prompts** (`config/prompts/.../*_team.j2` + base extractor
  prompt): instruct the model to emit a 1-line rationale per field WITHOUT dumping
  PHI verbatim beyond what `occurrences` already hold.
- It requires a **pipeline re-run** to populate the new field (cloud cost) and a
  **re-gate** (schema-loader, scorer, existing M-gates that assert section shapes).
- Token/cost: per-field rationales materially grow extractor output — measure.

**UPDATE (cheaper than first scoped — UI half is already DONE):** the per-field
rationale mechanism already exists and is proven on `report_metadata` (its prompt
§13 "Provenance contract" makes the model emit `provenance: {field → {block_id,
page, type, rationale}}`). The UI is already generic — `evidence.field_rationale_map`
reads ANY `provenance` map anywhere in the envelope, and the field-trace extraction
step shows it. So B1 is NOT new machinery; it's **replicating the metadata
provenance pattern to the umbrella sections**. The other team prompts today emit
only `occurrences` (location), not `rationale` (why).

Concrete steps:
1. Schema (`config/schemas/genomic_pathology_v3.json`): add the optional
   `provenance` object (identical shape to report_metadata's, ~line 56) to the
   biomarker/finding/specimen/clinical records.
2. Prompts: copy the metadata_team.j2 "Provenance contract" block into
   `molecular_biomarker_team.j2`, `specimen_findings_team.j2`,
   `clinical_info_team.j2` — keep `occurrences`, ADD a `provenance` map with a
   per-field `rationale`.
3. Re-run a doc through v3 to populate; re-gate `verify_phase2_m1` (section shapes).
   UI: zero change (already consumes any provenance map).

**Original proposed approach (superseded by the UPDATE above):**
1. Add an optional `provenance` block (or per-field `extraction_rationale`) to the
   umbrella sections in the v3 schema, exactly like report_metadata's provenance
   map (`{field: {block_id, page, type, rationale}}`). Reuse, don't invent.
2. Update the team prompts to fill it: one short clause per field, grounded in the
   cited block; "no rationale → omit" rather than hallucinate.
3. UI already supports it: `evidence.field_rationale_map()` + the field-trace
   extraction step read `provenance[field].rationale`. Extending the provenance
   map to umbrellas means biomarker/specimen fields light up with zero UI changes.
4. Re-run a doc through v3, re-gate (verify_phase2_m1 section shapes,
   verify_phase3_m8*), eyeball the Entity browser trace.

**Acceptance:** selecting any biomarker/specimen/clinical field shows a real
field-level extraction "why", same as report-metadata fields do today.

---
