# Migration Plan — Align to mCODE `genomic_pathology_extraction` v2 (schema v4)

**Status:** ✅ ALL 6 DECISIONS LOCKED (2026-05-27) — ready to implement V4-M0 onward
(see Decision Log + Milestones). Build behind a gate per milestone.

**Source of the new schema:** the local schema photos in `prod_schema_2/` (mCODE
`genomic_pathology_extraction` v2). This plan is the maintained record — revisit and
update it as decisions are locked and milestones land.

---

## 1. The target schema (what the new schema is)

Top-level key: **`genomic_pathology_extraction`**, with **four** sections:

1. **`report_metadata`** (flat object) — `report_title`, `Patient_MRN`, `Patient_First_Name`,
   `Patient_Last_Name`, `Patient_DOB`, `Vendor_Name`, `Collection_Date`, `Received_Date`,
   `Report_Date`, `Test_Name`, `Procedure`, `Ordering_Provider_First_Name`,
   `Ordering_Provider_Last_Name`, `Ordering_Provider_NPI`, `Ordering_Provider_Title`,
   `Practice_Name`, `Practice_NPI`, `Practice_Street_Address_1`, `Practice_Street_Address_2`,
   `Practice_City`, `Practice_State`, `Practice_ZIP_5digit`, `Practice_ZIP_4digit`,
   `llm_confidence_score`; plus document-level `total_pages` + `count_of_extracted_objects`.
   _(Per D4, v4 also keeps 5 extra fields — `Report_Type`, `Accession_Number`,
   `Signing_Pathologist`, `Ordering_Provider_Phone`, `Additional_Provider_Name` — which are
   NOT in the target; the scorer ignores them and `to_production` drops them.)_
2. **`Genomic_Variant_umbrella`** — `count_of_Genomic_Variants`, `llm_confidence_score`,
   `Genomic_Variants[]` each: `page_number`, `gene_studied`, `method`, `result`,
   `variant_allele_frequency`, `dna_change_type`, `amino_acid_change_type`,
   `genomic_dna_change`, `genomic_reference_sequence`, `coding_dna_change`,
   `transcript_reference_sequence`, `amino_acid_change`, `amino_acid_reference_sequence`,
   `clinical_significance`, `genomic_source_class`,
   `human_reference_sequence_assembly_version`, `allelic_state`, `chromosome_identifier`,
   `genomic_position`, `exon`.
3. **`other_molecular_biomarker_umbrella`** — `count_of_other_molecular_biomarkers`,
   `llm_confidence_score`, `other_molecular_biomarkers[]` each **flat**:
   `page_number`, `biomarker_name`, `method`, `result`, `reference_range`, `interpretation`.
4. **`tested_biomarker_umbrella`** — `count_of_tested_biomarkers`, `page_numbers`,
   `llm_confidence_score`, `tested_biomarkers[]` (alphabetized, **unique** gene/marker names).

**Cross-cutting rules:** VERBATIM vs DERIVED field tags; concatenate non-adjacent pieces
of one field with **` | `** (pipe-space-pipe); missing = JSON `null` (never `"N/A"`/`""`/`"None"`);
each `count_*` must equal its list length; per-section `llm_confidence_score` in [0,1].

> **Confirm before locking the schema:** exact field casing (`gene_studied` etc.) and the
> `dna_change_type` / `amino_acid_change_type` enum values still to be transcribed from the
> remaining `prod_schema_2/` example images.

---

## 2. Gap summary (have / partial / missing)

| Target piece | Status | Action |
|---|---|---|
| `report_metadata` | HAVE (flat) | Align; decide on 5 extra fields (D4). |
| `tested_biomarker_umbrella` | HAVE | Add alphabetize + unique + `page_numbers`/count rules. |
| Variant fields (HGVS, VAF, source class, assembly, exon) | HAVE — wrong place | Move out of `variant_detail` into a top-level section. |
| `Genomic_Variant_umbrella` (separate section) | MISSING (was merged away) | Revive (D1). Retired `genomic_variant_team.j2` + pre-merge `genomic_pathology_v2.json` exist. |
| `other_molecular_biomarker` flat shape | WRONG SHAPE | Flatten from two-level findings model (D2). |
| `significant_findings` / `clinical_information` | HAVE — not in target | Disable via config flag, do not delete (D3 — LOCKED). |
| Reusable infra (graph, triage/repair, VMAW, verifiers, UI, NER, normalizers, hgnc/hgvs) | HAVE | Re-bind to new sections only. |

---

## 3. Change / Add inventory (by component)

- **Schema** `config/schemas/genomic_pathology_v4.json` (new): the 4 sections above; re-add
  `Genomic_Variant_umbrella`; flatten `other_molecular_biomarker`; keep `significant_findings`
  + `clinical_information` present-but-disable-able (D3); top key `genomic_pathology_extraction`.
- **Section enable/disable config** (NEW — D3): a declarative switch (e.g. per-team
  `enabled: true|false` in `config/teams.yaml`, default true) read by the planner + graph
  team-set + scorer + recall-floor, so `significant_findings` / `clinical_information` are OFF
  by default but revivable by flipping the flag. Code, prompts, schema sections stay intact.
- **Teams** `config/teams.yaml` + `teams/`: revive `genomic_variant_team`
  (`section: Genomic_Variant_umbrella`); rewrite `molecular_biomarker_team` prompt to the flat
  shape; mark `specimen_findings_team` + `clinical_info_team` `enabled: false`.
- **Prompts** `config/prompts/`: un-retire `genomic_variant_team.j2` (refresh fields/enums);
  simplify `molecular_biomarker_team.j2` (drop findings/occurrences/variant_detail/biomarker_class);
  `tested_biomarker_team.j2` (+ alphabetize/unique/concatenation); base prompts (` | ` concat rule).
- **Planner** `agents/planner.py`: activate `genomic_variant_team`; honor the enable/disable flag.
- **NER + profiler** `config/ner_mapping.yaml`, `config/prompts/preprocess/block_profiler.j2`:
  re-add `Genomic_Variant_umbrella` routing target; gate specimen/clinical hints behind the flag.
- **Linker + registry** `config/link_registry.yaml`, `agents/linker.py`: drop biomarker↔finding
  within-record + biomarker↔specimen links; add `Genomic_Variant`↔`tested_biomarker` and
  `other_molecular_biomarker`↔`tested_biomarker` (the findings-based tested rule).
- **Attribution / normalization / recall-floor** configs: drop specimen-attribution targets;
  retarget recall-floor rules to the 4 active sections; keep normalizer map (dates/method/gene).
- **Scorer** `scripts/score_against_ground_truth.py`: score the 4 sections; honor the disable
  flag (don't score disabled sections); remove `variant_detail`-within-biomarker scoring.
- **Ground truth** `ground_truth/`: rebuild `demo.json` (and fixtures) to the 4-section shape.
- **Production mapping** `transform/to_production.py`, `config/production_mapping.yaml`: retarget
  to the new shape (likely near-identity since the new schema *is* the production shape).
- **UI** `ui/phase1/…`: update section references (views, evidence, production browser).
- **Docs** `docs/architecture/{schema,components}.md`, `docs/reference/traceability.md`: update.
- **Gates** `scripts/gates/`: new `gate_v4_*` set locking each milestone; update phase-2/3 gates
  whose fixtures assume the old section shapes.

---

## 4. Milestones (gated, additive — build behind a gate each)

| # | Milestone | Gate |
|---|---|---|
| V4-M0 | ✅ DONE — schema field list + semantics locked (§6) from Field-Definitions + 3 example outputs | (design only) |
| V4-M1 | ✅ DONE — `genomic_pathology_v4.json` (Draft-07 valid; `schema_loader` builds models for all 6 sections + round-trips Genomic_Variant/flat-biomarker/tested), `core/section_toggle.py` (enable/disable helper, default-true), `scripts/gates/gate_v4_m1_schema_sections.py` **PASS** (run natively; full sweep 33/33). (Flag *honoring* in planner/graph/scorer is V4-M4.) | `gate_v4_m1_schema_sections` ✅ |
| V4-M2a | ✅ DONE — revived `config/prompts/genomic_variant_team.j2` (real prompt for Genomic_Variant_umbrella: gene_studied + 20 fields, verbatim HGVS + hgvs_validate, ` \| ` concat, null discipline), new `config/teams_v4.yaml` (schema_path→v4; adds genomic_variant_team→Genomic_Variant_umbrella w/ hgnc+hgvs; specimen+clinical `enabled:false`; v3 teams.yaml untouched), `scripts/gates/gate_v4_m2_variant_team.py` **PASS** (run natively; full sweep 33/33). | `gate_v4_m2_variant_team` ✅ |
| V4-M2c | ✅ DONE (detector) — **HGVS structural-validity floor** `verification/hgvs_validity.py`: prefix-aware `hgvs_field_valid` + `find_malformed_hgvs` — flags genuinely-garbled HGVS (`c.18_garbled!!`) while NOT flagging legit prefix-less verbatim (`V617F`, `1849G>T`, via a field-appropriate c./p./g. retry). Routes to **needs_review/escalate** (no canonical → never `renormalize`). Gate `gate_v4_m2_variant_team` §7 **PASS**; full sweep **33/33**. **Suite/triage wiring deferred to V4-M5/M6** (when the v4 graph is assembled). Complements the HGNC gene canonicalization (V4-M2b) and the existing HGVS-normalization-diff (carries to v4 via the section-agnostic NormalizationVerifier). | `gate_v4_m2_variant_team` ✅ |
| V4-M2b | ✅ DONE — NER routing (`config/ner_mapping.yaml`: GENE/AMINO → Genomic_Variant_umbrella, additive), block-profiler vocab (`block_profiler.j2` offers Genomic_Variant_umbrella + routes variant rows to it). Planner needs **no change** (generic: activates a team when its `schema_section` is signalled). **Revived the dropped HGNC verification** (D-emphasis): added `hgnc` normalizer adapter (`agents/normalizer_hooks.py`, conservative — only exact alias canonicalization auto-flags) + `gene_studied`/`gene_symbol → hgnc` in `config/normalizer_map.yaml`, so the section-agnostic `NormalizationVerifier` now flags e.g. `JAK-2→JAK2`, `HER2/neu→ERBB2` on the variant gene. Gate `gate_v4_m2_variant_team` extended (§5 routing, §6 HGNC) **PASS**; updated `gate_p3_m1` assertion (vocab now offers Genomic_Variant_umbrella). Full sweep **33/33**. HGVS leaves (coding/genomic/amino) already covered by the section-agnostic verifier — carry to v4 unchanged. | `gate_v4_m2_variant_team` ✅ |
| V4-M3 | ✅ DONE — new `config/prompts/molecular_biomarker_team_v4.j2` (FLAT 6-field, routes variants away; v3 `molecular_biomarker_team.j2` untouched), `teams_v4.yaml` repointed, `gate_v4_m3_biomarker_flat.py` **PASS**. | `gate_v4_m3_biomarker_flat` ✅ |
| V4-M4 | ✅ DONE — `filter_enabled_keys` wired into `graph_linear.build_graph_dependencies` (subtractive, order-preserving, v3-safe no-op so v2/v3 unchanged); `runner.py` + `process_local.py` admit `version="v4"` (binds `genomic_pathology_v4.json` + `teams_v4.yaml`, active team set = `enabled_team_keys`, reuses the self-correcting graph + recursion backstop); `specimen_findings_team`/`clinical_info_team` dropped from the active set, their sections neither required nor scored. `gate_v4_m4_section_toggle.py` **PASS** (via /tmp mirror — FUSE read-deadlock recurred on freshly-written files). | `gate_v4_m4_section_toggle` ✅ |
| V4-M5 | ✅ DONE — new `config/link_registry_v4.yaml` (4 active links: `tested_to_result`, `variant_on_panel` re-pointed to the revived **Genomic_Variant_umbrella**↔tested, `variant_superseded_by`, `superseded_by`; ALL significant_findings/clinical_information links DROPPED since those sections are disabled); v3 `link_registry.yaml` untouched (D5). `build_graph_dependencies` gained `link_registry_path` (default v3); runner v4 branch binds the v4 registry. `validate_against_schema(v4)` clean. `gate_v4_m5_linker.py` **PASS** (run natively). | `gate_v4_m5_linker` ✅ |
| V4-M6 | ✅ DONE — **HGVS-validity floor WIRED**: `find_malformed_hgvs` runs in `run_verifier_suite` as an advisory `hgvs_validity` scorecard (passed=True); `triage.build_defects` turns its field_errors into `invalid_hgvs` defects, added to `_ESCALATE_ONLY` (no canonical → SME, never renormalize/re-extract). Disabled-section noise control: pure `drop_disabled_section_errors` drops ADVISORY misses (recall_floor/attribution/normalization/hgvs_validity) for disabled sections — schema_validator never touched, no-op for v2/v3. `disabled_sections` threaded deps→self-correcting verifier node→suite; `document_received_v3` admits v4; revived HGNC `gene_studied→hgnc` (M2b) re-asserted. `gate_v4_m6_verifier_configs.py` **PASS** (functional); p3 recall_floor + partial_accept gates still **PASS** (no v3 regression). | `gate_v4_m6_verifier_configs` ✅ |
| V4-M7 | ✅ DONE — scorer gained a `Genomic_Variant_umbrella` array block (gene-keyed by gene_studied + coding/amino change, scores the verbatim variant fields via `_VARIANT_SCORE_FIELDS`) — purely additive, skipped for v3 envelopes; flat-biomarker score set gained `reference_range`. New `ground_truth/demo_v4.json` (JAK2 V617F → Genomic_Variant_umbrella; flat biomarkers empty; significant_findings/clinical_information OMITTED). `SchemaValidator.validate_envelope` gained `disabled_sections` (skips a legitimately-absent disabled section — threaded from `run_verifier_suite`; default None → v2/v3 unchanged). `gate_v4_m7_scoring.py` **PASS** (self-score=perfect incl. variant; disabled not scored; v3 scoring intact); updated `gate_p2_m8` exact verifier-set to include the new `hgvs_validity`; p2_m6/m7, p3_m6/m10e still **PASS**. | `gate_v4_m7_scoring` ✅ |
| V4-M8 | ✅ DONE — `transform/to_production._biomarkers_findings` is now shape-tolerant: v3 nested `findings[]` unchanged; v4 FLAT records map 1:1 (the record IS the finding); the SEPARATE v4 `Genomic_Variant_umbrella` is folded into the SAME `pathology_biomarkers_findings` section (gene_studied→biomarker_name, HGVS→`details` via `_fold_variant`, clinical_significance→interpretation) — so the production contract is unchanged. `production_label` gained Genomic_Variant_umbrella ref mapping. v3 output byte-identical (v3 always has findings[] + no variant section). `gate_v4_m8_production.py` **PASS**; v3 `gate_p3_m9` still **PASS**. | `gate_v4_m8_production` ✅ |
| V4-M9 | ✅ DONE — `extraction_v2_view.py` adds a 🧬 Genomic_Variant_umbrella render block (separate variants), makes the biomarker render FLAT-tolerant (v3 nested findings[] OR v4 flat record), and GUARDS clinical_information + significant_findings behind `in envelope` so a disabled v4 section renders nothing. `overview.py` counts variants from BOTH the v3 findings path and the v4 Genomic_Variant_umbrella + reads the v4 `count_of_other_molecular_biomarkers`. Both views compile. `gate_v4_m9_ui.py` **PASS**. | `gate_v4_m9_ui` ✅ |
| V4-M10 | ✅ DONE — verified the field discipline is enforced CENTRALLY in `config/prompts/system/extractor.j2` (VERBATIM, DERIVED, the `" | "` space-pipe-space concat rule, null-not-fabricate, the `count_*` invariant) and `{% include %}`'d by all 4 v4-active team prompts; the two v4 ARRAY prompts (genomic_variant, molecular_biomarker_v4) restate the concat + `count == length` rules. No prompt rewrite needed (M2a/M3 already authored to spec). v3 nested biomarker prompt untouched. `gate_v4_m10_field_rules.py` **PASS**. | `gate_v4_m10_field_rules` ✅ |
| V4-M11 | ✅ DONE — added a **V4 gates** section to [`docs/reference/traceability.md`](../reference/traceability.md) (M1–M10 gate↔source↔doc rows + the non-destructive note + `make run-local PHASE=4`), and a **§7 v4** section to [`docs/architecture/schema.md`](../architecture/schema.md) (v3→v4 diff table: separate variants, flat biomarker, disabled sections, HGVS-validity floor) plus a shape-tolerant `to_production` note. This plan's milestone table is the live log. | (docs) |

### First v4 end-to-end (2026-05-27) — fix applied

`make run-local PHASE=4` on demo.pdf selected the v4 path correctly (v4 schema loaded;
planner active set = `metadata_team, genomic_variant_team, molecular_biomarker_team,
tested_biomarker_team`; disabled sections skipped). tested + biomarker committed; metadata
recovered a first-pass wrap/validate miss (pre-existing v3 behavior). **Fatal:**
`genomic_variant_team` extractor hit `MAX_ITERATIONS=12` — a tool-happy ReAct loop that
called `hgnc_normalize`/`hgvs_validate` every turn without ever emitting a Final Answer.

Fix (two parts, both safe for v2/v3):
1. `agents/extractor.py` — **force a Final Answer on the last two iterations**: drop the
   tools and demand JSON, so no team can burn the whole budget without answering (a
   converging team finalizes earlier, so it's unaffected; offline extractor gates pass).
2. `config/prompts/genomic_variant_team.j2` — a **tool-budget** rule: call each tool at
   most once per gene/change, then STOP and emit the object (a `null` tool result is a
   final answer, not a retry trigger).

Re-run #1 confirmed the loop fix: `genomic_variant_team` now finalizes (committed). New
failure: verdict `sme_flag` from **1 structural schema error** — the model emitted
`page_number: [1]` (a list) for the variant, conflating the singular scalar `page_number`
with the plural `page_numbers` (tested-umbrella). Strict Pydantic rejects the whole
section; re-extraction repeated the tic 3× → SME.

Fix (two parts, safe for v2/v3 — page_number is a scalar in every section):
1. `agents/extractor.py` — `_coerce_scalar_tics(payload)` runs before schema validation:
   unwraps a 1-element list for known scalar keys (`page_number`: `[1]`→`1`, `[]`→null).
   Verified offline: the exact failing payload now validates.
2. `config/prompts/genomic_variant_team.j2` + `molecular_biomarker_team_v4.j2` — state
   `page_number` is a SINGLE integer, NEVER a list.

Re-run #2 confirmed the page_number fix (variant team committed, no debug dump). New
failure: still `sme_flag` — but the assembled envelope was **missing
`Genomic_Variant_umbrella` entirely** while still emitting empty `significant_findings` +
`clinical_information` placeholders. Root cause: `Linker._assemble_envelope` was hardcoded
to the v3 five-section set — the missing ASSEMBLY half of M5 (M5 had only retargeted the
link *registry*).

Fix (gated on the v4 schema → v2/v3 assembly byte-identical):
- `agents/linker.py` — `Linker(assemble_sections=...)`: when set, assemble EXACTLY the
  active teams' sections (revived Genomic_Variant_umbrella included; disabled sections
  omitted) with schema-shaped empty placeholders; `_count_objects` now counts variants.
  Default None → the legacy v3 5-section assembly, unchanged.
- `pipeline/graph_linear.build_graph_dependencies` — passes `assemble_sections` = the
  active teams' `schema_section`s, only when bound to `genomic_pathology_v4.json`.
- `gate_v4_m5_linker` extended ([4b]) to lock: envelope includes the variant section,
  omits disabled, counts the variant; v3 default assembly unchanged. **PASS** (+ p2_m5,
  p3_m5, p2_m8 still PASS).

Verified offline: assembling the demo_v4 sections now validates with 0 structural errors.

### UI/trace bugs from the third e2e (2026-05-27)

User looked at the Production browser and flagged three real issues:

1. **JAK2 appeared twice** — once in the variant section (correct) and once in the flat
   biomarker section (duplicate). Root cause: `config/ner_mapping.yaml` still routed
   `GENE_OR_GENE_PRODUCT` + `AMINO_ACID` to `other_molecular_biomarker_umbrella` (v3
   legacy). The biomarker team's CoverageAuditor saw JAK2 in its parser_hypothesis →
   flagged a "missed biomarker" → Arbiter forced a re-extract → duplicate. Fix: new
   `config/ner_mapping_v4.yaml` (genes/aminos route to variant + tested only),
   threaded through the runner `_VERSIONS` registry + `build_graph_dependencies`. v3
   mapping untouched.
2. **Zero Linker links + missing linking phase in the trace.** The deterministic
   gene-key seed (`Linker._gene_key_links`) only walked `other_molecular_biomarkers`
   for `variant_detail` (v3 merge shape). It never knew about the revived v4
   `Genomic_Variant_umbrella.Genomic_Variants[].gene_studied`, so for v4 it always
   emitted zero links → the linking step never appeared in `assemble_field_trace`.
   Fix: added a v4 walk over Genomic_Variants in the seed (v3 path untouched).
   Verified: now emits `variant_on_panel JAK2-variant ↔ JAK2-panel` with rationale
   "Same HGNC-normalized gene 'JAK2'." `gate_v4_m5_linker` extended to lock the seed.
3. **Per-field rationale was patchy.** The Extractor's `provenance.rationale`
   already flows through `field_rationale`; the Auditor/Arbiter notes promote to
   field-scope when they singled out a field, else fall back to section. With links
   now firing, the linking-phase per-link rationale (the "Same HGNC-normalized gene"
   line) reaches the field timeline. The remaining gap — binding items keyed by
   `candidate:X` (NER-orphan probes) not matching envelope refs — is a smaller polish
   item the binding-verifier output shape needs to settle.

### Complete agent trace (2026-05-28)

User asked the sharp question: "when we show agentic trace are we showing all agents
invoked?" — honest answer was no. `agent_trace.json` contained only team-internal
agents (Extractor / CoverageAuditor / Arbiter / Extractor re-extract). The UI's
`assemble_field_trace` merged extra channels (scorecards, links, repair_log, vmaw_log)
at render time, but Planner / Triage / Dedup / Supersession / DecisionRouter never
appeared anywhere — and reading `agent_trace.json` directly never showed them.

**Fix.** Added `core/trace_recorder.py` (a tiny `record(...)` + `extend_trace(...)`
builder pair + a `filter_by_ref(...)` pure-filter), and wired one trace-record call
into every cross-section node:

| Node | What it records |
|---|---|
| Planner | active vs skipped teams + rationale |
| teams_node | preserves Planner's record, appends per-team Extractor / Auditor / Arbiter / re-extract |
| Linker | assembly summary + one record per emitted link (carries the rationale) |
| verifier_node (graph_selfcorrecting) | one record per scorecard + a LinkBindingVerifier record |
| Triage | decision summary + one record per repair_request + one per escalation |
| Repair | one record per applied repair action (with team / target_ref) |
| VMAW | one record per resolution step (EC / CITE / VA) with the item's ref |
| DecisionRouter | the final verdict, with refs covering accepted + flagged sections |

Now one `agent_trace.json` = the full trip. `assemble_field_trace` can be simplified
to a pure filter over this unified list in a future pass.

New gate `gate_v4_m13_trace_completeness.py` locks: recorder primitives, the planner
record, the triage records, the DecisionRouter record, the linker per-link records,
and `filter_by_ref`'s array-token matching. **PASS.** Regression sweep across
p2_m5/m8, p3_m4/m5/m6/m7/m8b/m8c, v4_m4/m5/m6/m12 — all green.

### De-hardcode pass (2026-05-28) — dedup is the canonical correctness layer

User pushed back on the architectural choice ("why restrict at extractor? JAK2 should
be resolved by alias or dedup"). They were right. Extraction stays recall-first;
correctness moves downstream.

What changed:
- **Reverted** the v4 NER-routing restriction. `config/ner_mapping.yaml` is now the
  single mapping for v2/v3/v4 — genes route to all umbrellas, as before.
- **Added `config/dedup_policy.yaml`** — declarative cross-section dedup. The single
  rule says: "Genomic_Variant_umbrella owns gene sequence changes; if the same
  HGNC-canonical gene appears in `other_molecular_biomarker_umbrella` AND that row
  carries `findings[*].variant_detail`, drop the biomarker row." Adding a new dedup
  rule is one YAML entry, no Python.
- **`Linker._apply_dedup_policy`** — the canonical reconciliation, runs after assembly
  and supersession, before the count. Records `notes: "dedup -N"` for observability.
- **`Linker._gene_key_links`** is now fully registry-driven — iterates
  `link_registry.active_types()` and reads each section's `gene_key_field` (and
  optional `gene_key_requires` filter) from `config/section_layout.yaml`. No v3/v4
  branches, no hardcoded `Genomic_Variant_umbrella` / `Genomic_Variants` /
  `gene_studied` literals. Adding a new gene-key pairing is two config rows.
- **`_canon` is forgiving of trailing context** — `"JAK2 V617F Mutation"` → `JAK2` via
  first-token fallback. Recall-friendly without sacrificing HGNC correctness.
- **`_SCALAR_TIC_KEYS` moved out of `agents/extractor.py`** into
  `config/section_layout.yaml:scalar_keys`. Adding a key is one YAML line.
- Lazy-load: `Linker()` with no explicit registry still works in legacy validation
  mode (gate-friendly), but the seed lazy-loads `config/link_registry.yaml` so it has
  a vocabulary. The v4 run path passes the v4 registry explicitly.

New gate: `gate_v4_m12_dedup_policy.py` locks the contract — duplicate dropped, plain
IHC preserved, v3 no-op, canon forgiving, scalar_keys config-driven. **PASS.** Full
sweep on the touched surfaces (p2_m5/m8, p3_m2/m3/m5/m6/m9, v4_m5/m6/m7/m8/m9/m10/m12)
green.

Re-run `make run-local PDF=./data/actual_docs/demo.pdf PHASE=4`. Expected: no JAK2
duplicate (dedup catches it post-assembly even though biomarker extraction wasn't
restricted), variant_on_panel link present (registry-driven seed), `notes: dedup -1`
in the linker output.

### Dynamic refactor (2026-05-27) — adding a team is now a pure config change

Stress-tested the "is this really dynamic?" question and retired the remaining hardcoded
shape paths the v4 migration kept tripping over. **41/41 gates green** (p2 + p3 + v4); v3
behaviour preserved (the two v3 gates that locked the *old* hardcoded shape were updated
to reflect the new generic contract — that was the point of the refactor).

What's now config:
- **`Linker._assemble_envelope`** is fully generic — it emits exactly the sections the
  active teams produced (in registry order). No hardcoded 5-section path, no
  `assemble_sections` plumbing through `build_graph_dependencies`. `_count_objects` is
  driven by **`config/section_layout.yaml`** (a tiny per-section map declaring each
  umbrella's record-array name + empty placeholder). Adding a new section is one YAML row.
- **`pipeline/runner.py`** dispatch is a **`_VERSIONS` table** (`{kind, schema, teams,
  registry}` per version). Adding a new pipeline version is one row, not a new `elif`
  branch.
- Removed `Linker(assemble_sections=…)` param; updated `gate_v4_m5_linker` [4b] to lock
  the generic contract (envelope == produced sections + count; revived variant present;
  disabled omitted; count from `section_layout.yaml`).

Honest scope: `to_production`, the UI views, and the per-version scorer still carry
shape-specific code — those are *semantic* mappings (the variant→biomarker fold for
production, the per-section rendering) rather than mechanical assembly. They can be made
more generic in a future pass; for now they were made shape-tolerant in M8/M9/M7. The
linker + runner were the highest-leverage targets (every v4 milestone touched them).

Re-run `make run-local PDF=./data/actual_docs/demo.pdf PHASE=4` to confirm.

---

## 5. Decision Log

Each decision is locked here before its milestone is built.

### D1 — Re-introduce `Genomic_Variant_umbrella` + revive `genomic_variant_team`
**Status:** ✅ LOCKED (2026-05-27). **Decision:** Revive the retired `genomic_variant_team`
(un-retire `genomic_variant_team.j2`, re-add `Genomic_Variant_umbrella` as a top-level
section) — reuse the proven pre-merge structure, refresh fields/enums to the new schema.

### D2 — Flatten `other_molecular_biomarker` to the 6-field shape
**Status:** ✅ LOCKED (2026-05-27). **Decision:** Flatten the **internal** schema to the
6-field shape `{page_number, biomarker_name, method, result, reference_range, interpretation}`
to match prod exactly. Drop the two-level `findings[]/occurrences[]/variant_detail/
biomarker_class/supersession` model (accepted loss of multi-statement + amendment tracking).

### D3 — `significant_findings` + `clinical_information` scope
**Status:** ✅ LOCKED (2026-05-27). **Decision:** Keep the code, prompts, and schema sections
intact but **disable them via a config flag** (default OFF), so they can be revived later by
flipping the flag. Do NOT retire/delete. Mechanism: per-team `enabled` flag in
`config/teams.yaml` honored by planner + graph team-set + scorer + recall-floor.

### D4 — `report_metadata` extra fields
**Status:** ✅ LOCKED (2026-05-27). **Decision:** KEEP the 5 extra fields (`Report_Type`,
`Accession_Number`, `Signing_Pathologist`, `Ordering_Provider_Phone`,
`Additional_Provider_Name`) in v4 even though the target omits them.
**Implication:** the scorer must ignore (not penalize) fields absent from ground truth, and
`to_production` must drop these extras when emitting the prod-parity output.

### D5 — Versioning
**Status:** ✅ LOCKED (2026-05-27). **Decision:** New `config/schemas/genomic_pathology_v4.json`;
keep v3 + all its gates passing during migration; switch the default to v4 only when the
v4 gates are green.

### D6 — `prod_schema_2/` handling
**Status:** ✅ LOCKED (2026-05-27). **Decision:** `prod_schema_2/` added to `.gitignore`
(local-only, same discipline as `prod_prompt_data/`). Schema photos are never committed.

---

## 6. Locked field list + semantics (V4-M0)

Authoritative, transcribed from the Field-Definitions image + 3 example outputs
(Examples 1, 3). All field names are **snake_case** exactly as below. Missing = JSON
`null` (never `"N/A"`/`""`/`"None"`). `[VERBATIM]` = copy as printed; `[DERIVED]` = computed.

**`report_metadata`** (flat): `report_title`, `llm_confidence_score`, `Patient_MRN`,
`Patient_First_Name`, `Patient_Last_Name`, `Patient_DOB`, `Vendor_Name`, `Collection_Date`,
`Received_Date`, `Report_Date`, `Test_Name`, `Procedure`, `Ordering_Provider_First_Name`,
`Ordering_Provider_Last_Name`, `Ordering_Provider_NPI`, `Ordering_Provider_Title`,
`Practice_Name`, `Practice_NPI`, `Practice_Street_Address_1`, `Practice_Street_Address_2`,
`Practice_City`, `Practice_State`, `Practice_ZIP_5digit`, `Practice_ZIP_4digit`.
Document-level (siblings of the umbrellas): `count_of_extracted_objects` [DERIVED],
`total_pages` [DERIVED]. (Plus the 5 D4 extras, internal-only.)
- **Patient_MRN rule:** only a value explicitly labeled "Medical Record #", "MRN", "Med Rec #".
  Accession / case / "Your No" / redacted "ID#" are NOT the MRN → `null`.

**`Genomic_Variant_umbrella`**: `count_of_Genomic_Variants` [DERIVED],
`llm_confidence_score` [DERIVED], `Genomic_Variants[]` — each object (ALL fields always
emitted; `null` when not on the page):
`page_number` [DERIVED], `gene_studied`, `method`, `result`, `variant_allele_frequency`,
`dna_change_type`, `amino_acid_change_type`, `genomic_dna_change`, `genomic_reference_sequence`,
`coding_dna_change`, `transcript_reference_sequence`, `amino_acid_change`,
`amino_acid_reference_sequence`, `clinical_significance`, `genomic_source_class`,
`human_reference_sequence_assembly_version`, `allelic_state`, `chromosome_identifier`,
`genomic_position`, `exon`. `dna_change_type`/`amino_acid_change_type` are **open vocab**
("accepted values include but are not limited to …") → free string, not a closed enum.

**`other_molecular_biomarker_umbrella`**: `count_of_other_molecular_biomarkers` [DERIVED],
`llm_confidence_score` [DERIVED], `other_molecular_biomarkers[]` — each **flat**:
`page_number` [DERIVED], `biomarker_name`, `method` (IHC/NGS/FISH/Special Stains),
`result`, `reference_range`, `interpretation`. Includes protein expression (IHC) +
genomic-instability scores (TMB/MSI/LOH/HRD/ctDNA); EXCLUDES histological findings and
general lab markers.

**`tested_biomarker_umbrella`**: `count_of_tested_biomarkers` [DERIVED] (= list length),
`page_numbers` [DERIVED] (comma-joined, e.g. `"2,5,7"`), `llm_confidence_score` [DERIVED],
`tested_biomarkers[]` — **alphabetized, unique** gene/marker names. Include panels + gene
lists + findings-based names + IHC protein markers + genomic-instability markers; EXCLUDE
non-molecular stains (PAS, Giemsa, iron, reticulin, H&E) and enzyme stains (lysozyme).

**Cross-cutting:** concatenate non-adjacent pieces of one field with **` | `**
(pipe-space-pipe); each `count_*` MUST equal its list length; per-section
`llm_confidence_score` ∈ [0,1]; output wrapped under the top key
`genomic_pathology_extraction`.
