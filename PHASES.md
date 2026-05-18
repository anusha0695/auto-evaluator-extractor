# Agentic Ground-Truth Extraction — Master Phases Plan

**Project location (dev workspace):** `extractor/`
**Destination (production codebase):** copied at end of all 4 phases, Option A — four phase commits in destination git history
**Strategy:** Build all 4 phases here, snapshot each one as we go, transfer at end
**GCP project:** `medical-report-extraction`
**Schema (source of truth):** `config/schemas/genomic_pathology_v2.json`

---

## Workflow rules

1. **All development happens in `extractor/`.** Nothing is copied into the production codebase until all 4 phases are complete.
2. **Additive modularity.** Each phase adds new files. The pipeline graph is versioned (`graph_v1.py`, `graph_v2.py`, etc.) so no shared file gets rewritten across phases.
3. **Shipping manifests are kept live.** Every time we add, modify, or delete a file during Phase N development, the entry is logged in `MANIFEST_PHASE_N.md`. At the end of all phases, the user copies files into the production codebase using these manifests as the source of truth.
4. **UI per phase, independent.** Each phase has its own self-contained Streamlit app at `ui/phaseN/app.py`, runnable on its own port.
5. **Snapshot at end of each phase.** `snapshots/phaseN/` gets a frozen copy of that phase's UI and pipeline graph for replay demos.
6. **UI is read-only.** Pipeline runs happen out-of-band via `scripts/process_local.py` (local) or `scripts/deploy_dataflow.py` (production). UI browses already-processed results from BigQuery + GCS + Cloud Trace.
7. **GCP-native observability via OpenTelemetry → Cloud Trace + Cloud Logging + Cloud Monitoring.** No LangSmith — PHI stays in our project.
8. **Schema-independent engine.** JSON Schema files are the source of truth. Prompts are Jinja templates that pull from the schema. Same engine code works for any future extraction project — only `config/` changes.
9. **In-process medical NER.** SciSpaCy / MedSpaCy / HGVS / HGNC lookups all run as Python packages inside the worker. No separate Vertex Custom Prediction endpoint to operate. PHI never leaves the worker process.
10. **Always emit all 4 umbrella sections.** Even when a phase's TeamSet only populates one section, the output JSON is wrapped in the full schema-v2 envelope (`report_metadata`, `Genomic_Variant_umbrella`, `other_molecular_biomarker_umbrella`, `tested_biomarker_umbrella`) with empty placeholders for the others. Phase 1 output is forward-compatible with Phase 2+ schema validation.
11. **Dev-environment only — no deployment automation in any phase.** No Terraform, no Dataflow scripts, no `infrastructure/` directory. GCP resources (DocAI processor, BigQuery dataset, GCS bucket, Firestore) are **manually provisioned** by the platform/ops team as a one-time setup, documented in `README.md` § Prerequisites. The pipeline runs via `make run-local` (local dev) and `python scripts/process_local.py` (batch). When the system eventually goes to production scale, the receiving team handles deployment in their own way — that's out of scope here.

---

## Architecture recap

### Preprocessing layer (cached, runs once per PDF)
1. **DocAI Layout Parser** — structural parsing → `doc_profile.layout` (processor `pretrained-layout-parser-v1.6-2026-01-13`, ID `81e83f6783d90bb0`, location `us`)
2. **Fax-header filter** — deterministic regex stripper that removes fax-transport bands (driven by `demo.pdf`'s real-world quirks)
3. **Block Profiler** (Gemini 2.5 Flash, temperature 0.0) — semantic classification of every block, including labeling surviving fax noise as `fax_transport_noise` → `doc_profile.block_profiles`
4. **Medical NER** (in-process SciSpaCy: `en_ner_bionlp13cg_md` + `en_ner_bc5cdr_md` + `en_core_web_sm`; MedSpaCy negation + HGVS + HGNC alias lookup added in Phase 2+) — independent entity candidates → `parser_hypothesis`

### Orchestration
5. **Planner Agent** (Gemini 2.5 Pro, Plan-and-Execute, temperature 0.0) — reads preprocessing outputs, decides per-team page/block assignments and budgets, emits a plan

### Extraction — 4 Specialist Teams in parallel
Each team owns one of the schema-v2 umbrella sections:

- **MetadataTeam** → `report_metadata` (patient identity, vendor, dates, ordering provider, practice)
- **GenomicVariantTeam** → `Genomic_Variant_umbrella` (gene-level variants with HGVS, VAF, clinical significance, etc.)
- **MolecularBiomarkerTeam** → `other_molecular_biomarker_umbrella` (non-gene biomarkers: TMB, MSI, PD-L1, HRD, etc.)
- **TestedBiomarkerTeam** → `tested_biomarker_umbrella` (alphabetized list of every gene/biomarker on the panel, whether or not a variant was detected)

Each team has 3 agents internally:
- **Extractor** (Gemini 2.5 Pro, ReAct with tools) — finds entities
- **Coverage Auditor** (Gemini 2.5 Pro) — different prompt, hunts for misses (recall safety)
- **Arbiter** (Gemini 2.5 Flash, gated) — only fires on conflict, picks accept-Extractor / re-extract / invoke-VMAW

### Side-channel verifier
- **VMAW** (Gemini 2.5 Pro, ReAct fact-checker) — invoked by Arbiters on conflict, grounds disputed claims in PDF evidence, writes corrected entity back to state

### Linking
- **Linking & Resolution Agent** (Gemini 2.5 Pro, single call) — merges the 4 team outputs into the nested schema-v2 envelope, runs alias resolution (e.g. HER2 → ERBB2), computes the derived counts (`count_of_extracted_objects`, per-umbrella counts), enforces the alphabetized-uniqueness rule on `tested_biomarkers`

### Verification (deterministic, no LLM)
- **Schema Validation** (always-on) — Pydantic generated from `genomic_pathology_v2.json`
- **Coverage Audit** — count of extracted objects vs `parser_hypothesis`
- **Link Consistency** — variants ↔ panel, dates ordered (Collection ≤ Received ≤ Report), patient identity coherent across umbrellas
- **Evidence + Confidence** — every emitted field carries a page citation; per-umbrella `llm_confidence_score` ≥ threshold

### Decision routing
- Auto-Accept → BigQuery + GCS (target ~85% of docs)
- Fixable → loop back to Planner with hints
- Ambiguous → SME Review UI (target ~15% of docs)

---

## Cross-cutting principles

### Schema independence
- `config/schemas/genomic_pathology_v2.json` is the source of truth for what we extract
- Prompts in `config/prompts/*.j2` reference schema sections by name and pull field descriptions + `extraction_mode` (VERBATIM / DERIVED / VERBATIM_OR_INFERRED) automatically
- Pydantic models generated at runtime from the JSON Schema
- Gemini structured output uses the same JSON Schema as its `response_schema`
- For a future project (radiology, contracts, financials): replace `config/schemas/` and `config/prompts/`. Zero code changes.

### Storage configuration
`config/storage.yaml` maps each pipeline version to its persistence destinations:
```yaml
phase_1:
  artifacts_gcs_prefix: gs://patient_clinical_trial/extraction_outputs/phase1/
  bigquery_extractions_table: medical-report-extraction.extractor_dev.extractions_v1
  bigquery_runs_table:        medical-report-extraction.extractor_dev.runs_v1
phase_2: { ... }
phase_3: { ... }
phase_4: { ... }
```
Pipeline writes to and UI reads from the configured destinations. Move the bucket — change one line in YAML.

### Observability
- OpenTelemetry SDK instruments LangGraph nodes, agent calls, tool calls
- Traces → Cloud Trace (tagged with `doc_id` and `pipeline_version`)
- Structured logs → Cloud Logging
- Metrics → Cloud Monitoring (success rate, latency, cost per doc, SME escalation rate)
- All inside the GCP project, BAA-covered, PHI never leaves

### LangGraph state
- **Checkpointer:** Firestore (Native mode) in the same GCP project. Same BAA, same project boundary as everything else.

### Local-first execution
- `scripts/process_local.py` is the default dev tool — runs the pipeline on a developer machine against PDFs in GCS, writes to BigQuery and GCS
- `scripts/deploy_dataflow.py` packages the pipeline as a Dataflow job for production scale (Phase 4)
- Same pipeline code, two entry points

### Read-only UI
- UI lists documents that have already been processed (from BigQuery `runs_vN` table)
- User picks a document — UI fetches its persisted results and displays them
- No live processing during demos
- GCS browser shows files in the input bucket purely for reference — selection doesn't trigger processing

### Disambiguation: implementation phase vs schema section

- **Implementation phase** = a build sprint that ships a versioned slice of the system (Phase 1 / 2 / 3 / 4 → `graph_v1.py` / `graph_v2.py` / ...).
- **Schema umbrella section** = one of the four top-level groups in `genomic_pathology_v2.json` (`report_metadata` / `Genomic_Variant_umbrella` / `other_molecular_biomarker_umbrella` / `tested_biomarker_umbrella`), each owned by exactly one Specialist Team.

These never get confused: Phase 1 ships the MetadataTeam (which owns `report_metadata`); Phase 2 adds the other 3 teams + Planner + Linking; Phase 3 adds VMAW; Phase 4 adds the learning loop + production deploy.

---

# Phase 1 — Foundation + MetadataTeam end-to-end

## Goal
Prove the architecture works end-to-end on the simplest possible slice: preprocessing runs (DocAI + fax-filter + Block Profiler + in-process SciSpaCy NER), MetadataTeam extracts patient + provider + practice info into the schema-v2 `report_metadata` section, schema validates, result lands in BigQuery + GCS, UI shows it.

Phase 1 emits the **full schema-v2 envelope** with the other 3 umbrellas as empty placeholders, so the output is already forward-compatible with Phase 2.

## Primary validation doc
`data/actual_docs/demo.pdf` — real NeoGenomics fax (William Dawson, JAK2 V617F Quantitative).
Ground truth at `ground_truth/demo.json` covers all 4 umbrella sections (Phase 1 scores `report_metadata` only).

## Secondary regression fixtures
5 synthetic mock PDFs at `ground_truth/phase1/`:
- `doc_3`, `doc_11` — Tempus xT format (vendor inferred from "xT" product marker)
- `doc2_1`, `doc2_6`, `doc2_25` — unknown_vendor_v2 format (vendor absent, physician in footer)

## What we build

### Files added in Phase 1

#### Config
- `config/schemas/genomic_pathology_v2.json` — full v2 JSON Schema, all 4 umbrella sections (already authored as part of the Phase 1 pre-Block-A batch)
- `config/storage.yaml` — with `phase_1` entry
- `config/teams.yaml` — with `metadata_team` registered, mapped to schema section `report_metadata`
- `config/tools.yaml` — tool registry
- `config/prompts/system/extractor.j2` — base extractor system prompt
- `config/prompts/system/coverage_auditor.j2` — base auditor prompt
- `config/prompts/system/arbiter.j2` — base arbiter prompt
- `config/prompts/metadata_team.j2` — metadata-specific extraction prompt (encodes fax-header-filter rule, vendor-inference rule, practice-vs-vendor disambiguation, strict-MRN rule, ISO-date normalization)
- `config/prompts/preprocess/block_profiler.j2` — Block Profiler prompt
- `config/prompts/preprocess/medical_ner.j2` — Medical NER parser-hypothesis prompt

#### Core engine (schema-independent, frozen after Phase 1)
- `core/__init__.py`
- `core/schema_loader.py` — loads JSON Schema, generates Pydantic models at runtime
- `core/prompt_renderer.py` — Jinja renderer that pulls schema-section field descriptions + `extraction_mode` tags
- `core/state.py` — `PipelineState` TypedDict
- `core/errors.py` — typed exception family
- `core/observability.py` — OpenTelemetry → Cloud Trace + Logging + Monitoring wiring
- `core/checkpointer.py` — Firestore-backed LangGraph checkpointer factory
- `core/gcs_client.py` — GCS list + read operations
- `core/persistence.py` — reads `storage.yaml`, writes to BigQuery + GCS tagged by `doc_id` + `pipeline_version`
- `core/tool_registry.py` — shared tool definitions

#### Preprocessing (frozen after Phase 1)
- `preprocess/__init__.py`
- `preprocess/docai_parser.py` — DocAIParser class
- `preprocess/fax_header_filter.py` — deterministic regex stripper for fax-transport bands
- `preprocess/block_profiler.py` — BlockProfiler class (Gemini 2.5 Flash @ T=0.0)
- `preprocess/medical_ner.py` — SciSpaCyMedicalNER (in-process; loads `en_ner_bionlp13cg_md` + `en_ner_bc5cdr_md` + `en_core_web_sm` as Python packages)
- `preprocess/preprocess_node.py` — LangGraph orchestration node

#### Agents (Phase 1 set, base ones frozen after)
- `agents/__init__.py`
- `agents/base.py` — abstract `Agent` base class (all calls at T=0.0)
- `agents/extractor.py` — ReAct extractor agent
- `agents/coverage_auditor.py` — coverage auditor agent
- `agents/arbiter.py` — gated arbiter agent

#### Teams (one file per team)
- `teams/__init__.py`
- `teams/metadata_team.py` — MetadataTeam subgraph (extractor + auditor + arbiter)

#### Verification (Phase 1 partial)
- `verification/__init__.py`
- `verification/schema_validator.py` — Pydantic validation against the JSON Schema

#### Decision
- `decision/__init__.py`
- `decision/decision_router.py` — routes to Auto-Accept or SME flag based on verifier results

#### Pipeline composition (versioned, this one frozen after Phase 1)
- `pipeline/__init__.py`
- `pipeline/graph_v1.py` — frozen Phase 1 LangGraph composition (preprocessing → MetadataTeam → schema validator → decision → persist)
- `pipeline/runner.py` — entry point dispatching by `--version` flag

#### Scripts
- `scripts/process_local.py` — local pipeline invocation
- `scripts/snapshot_phase.py` — automates copying `ui/phaseN/` and `pipeline/graph_vN.py` into `snapshots/phaseN/`

#### UI (Phase 1, frozen after Phase 1)
- `ui/phase1/app.py` — Streamlit main shell, runnable as `streamlit run ui/phase1/app.py`
- `ui/phase1/results_browser.py` — lists processed docs from BigQuery `runs_v1`
- `ui/phase1/pages/overview.py` — doc metadata + run summary
- `ui/phase1/pages/preprocessing_view.py` — visualizes DocAI layout blocks (with `fax_transport_noise` class visible), SciSpaCy NER candidates highlighted
- `ui/phase1/pages/metadata_extraction_view.py` — extracted `report_metadata` JSON tree with page citations; special widget showing why `Patient_MRN=null` per the strict-MRN rule
- `ui/phase1/components/pdf_viewer.py` — basic PDF viewer (page-level)
- `ui/phase1/components/json_tree.py` — collapsible JSON tree with citations

#### Project-level
- `pyproject.toml`
- `requirements.txt`
- `.env.example`
- `README.md`
- `MANIFEST_PHASE_1.md` — live shipping manifest, updated continuously during Phase 1

#### Ground truth (Phase 1 validation set — already authored)
- `ground_truth/demo.json` — primary, all 4 umbrellas populated
- `ground_truth/phase1/doc_3.json`, `doc_11.json`, `doc2_1.json`, `doc2_6.json`, `doc2_25.json` — secondary, `report_metadata` only
- `ground_truth/phase1/README.md` — validation set documentation

### Phase 1 UI demonstrates
- Open browser → see list of already-processed docs in sidebar
- Click `demo` → see overview (filename, processed at, verdict, cost, latency)
- Click "Preprocessing" tab → see PDF with DocAI layout blocks color-coded by role (including the `fax_transport_noise` band at top of page 1), NER candidates highlighted
- Click "Metadata Extraction" tab → see extracted `report_metadata` JSON in tree view, every field links to its source page in the PDF
- See `Patient_MRN: null` with a tooltip explaining the strict-MRN rule
- See `Vendor_Name: "NeoGenomics"` linking to the masthead block
- See verdict: auto-accept or SME flag with reason

### Phase 1 acceptance criteria
- `demo.pdf` processed via `process_local.py --version v1` writes to BigQuery `extractions_v1` + GCS artifacts
- Extracted `report_metadata` matches `ground_truth/demo.json` on ≥ 80% of fields (acceptance gate)
- All 5 mock fixtures also pass ≥ 80% on `report_metadata`
- The Phase 1 Streamlit app browses the processed docs and renders the JSON
- Schema validation result visible in the UI
- OpenTelemetry traces visible in Cloud Trace
- LangGraph checkpoints visible in Firestore

### End of Phase 1
- Run `scripts/snapshot_phase.py --phase 1` → copies `ui/phase1/` + `pipeline/graph_v1.py` to `snapshots/phase1/`
- Finalize `MANIFEST_PHASE_1.md` — every file that was added, modified, or deleted
- Tag git: `v1.0-phase1`
- Demo to business team (lead with `demo.pdf` — the real-world story)

---

# Phase 2 — All 4 Specialist Teams + Planner + Linking + full Verifiers

## Goal
Complete the extraction layer. All 4 teams running in parallel, Planner dispatching, Linking assembling the schema-v2 envelope from the 4 team outputs, all 4 verifiers running. Phase 2 fills out the 3 umbrella sections that Phase 1 emitted empty.

## What we build

### Files added in Phase 2

#### Config
- `config/prompts/genomic_variant_team.j2` — Genomic_Variant_umbrella extraction (HGVS rules, alias resolution context for HGNC, exon/chromosome handling)
- `config/prompts/molecular_biomarker_team.j2` — other_molecular_biomarker_umbrella extraction (TMB / MSI / PD-L1 / HRD etc.)
- `config/prompts/tested_biomarker_team.j2` — tested_biomarker_umbrella extraction (alphabetized, deduplicated panel list)
- `config/prompts/planner.j2`
- `config/prompts/linking.j2`

#### Agents
- `agents/planner.py` — Plan-and-Execute Planner Agent
- `agents/linking.py` — Linking & Resolution Agent (runs HGNC alias resolution, computes counts, alphabetizes `tested_biomarkers`)

#### Teams (3 new files)
- `teams/genomic_variant_team.py`
- `teams/molecular_biomarker_team.py`
- `teams/tested_biomarker_team.py`

#### Preprocessing additions (in-process, no new infra)
- `preprocess/medspacy_negation.py` — activates MedSpaCy negation detection (needed to avoid treating "BRCA1 — not detected" as a positive finding)
- `preprocess/hgvs_validator.py` — wraps the `hgvs` Python package to canonicalize variant nomenclature
- `preprocess/hgnc_alias.py` — local HGNC alias lookup (HER2 → ERBB2 etc.)

#### Verification (3 new files)
- `verification/coverage_audit.py` — system-wide recall check vs `parser_hypothesis`
- `verification/link_consistency.py` — variants ↔ panel, date ordering (Collection ≤ Received ≤ Report), patient identity coherent across umbrellas
- `verification/evidence_confidence.py` — every field has page citation + `llm_confidence_score` ≥ threshold

#### Pipeline composition
- `pipeline/graph_v2.py` — frozen Phase 2 LangGraph composition (Planner → 4 teams in parallel → Linking → 4 verifiers → decision → persist)

#### UI (Phase 2, frozen after Phase 2)
- `ui/phase2/app.py` — Streamlit shell
- `ui/phase2/results_browser.py` — lists docs from BigQuery `runs_v2`
- `ui/phase2/pages/overview.py`
- `ui/phase2/pages/planner_view.py` — visualizes the Planner's emitted plan (per-team page/block assignments + budgets)
- `ui/phase2/pages/full_extraction_view.py` — full nested schema-v2 envelope from all 4 teams
- `ui/phase2/pages/verifier_scorecards.py` — all 4 verifiers' pass/fail with details
- `ui/phase2/components/pdf_viewer.py` — copy of Phase 1's, frozen
- `ui/phase2/components/json_tree.py` — copy of Phase 1's, frozen
- `ui/phase2/components/plan_visualizer.py` — new for Phase 2

#### Project-level
- `MANIFEST_PHASE_2.md` — live during Phase 2 development

### Files modified in Phase 2 (config files only — additive YAML/JSON)
- `config/storage.yaml` — append `phase_2:` block (BigQuery `extractions_v2` + `runs_v2`, GCS prefix `gs://patient_clinical_trial/extraction_outputs/phase2/`)
- `config/teams.yaml` — register 3 more teams (`genomic_variant_team`, `molecular_biomarker_team`, `tested_biomarker_team`)
- `pipeline/runner.py` — add `v2` route
- `requirements.txt` — any new dependencies (MedSpaCy + HGVS were installed Phase 1 for forward compat; verify no surprises)
- `README.md` — append Phase 2 demo instructions

### Phase 2 acceptance criteria
- `demo.pdf` and the 5 mock fixtures processed via `process_local.py --version v2` → BigQuery `extractions_v2`
- Full schema-v2 envelope populated and matching `ground_truth/demo.json` on all 4 umbrellas at ≥ 80% per-field match
- Hand-labelled `Genomic_Variant` / `tested_biomarker` ground truths added to the 5 mock fixtures during Phase 2 (currently empty); the Phase 2 acceptance gate covers these once added
- All 4 verifiers run on every doc, scorecard visible
- Planner's reasoning visible (which teams, which budgets)

### End of Phase 2
- `scripts/snapshot_phase.py --phase 2`
- Finalize `MANIFEST_PHASE_2.md`
- Git tag `v2.0-phase2`
- Demo to business

---

# Phase 3 — VMAW + chunk-level SME review for ambiguous cases

## Goal
Add the side-channel fact-checker. When an Arbiter inside a team can't decide a conflict, it invokes VMAW which grounds the disputed claim in actual PDF evidence. UI gains chunk-level highlighting so SME reviewers can click a flagged item and the PDF viewer jumps to the exact bbox (using DocAI's cached layout coordinates).

## What we build

### Files added in Phase 3

#### Agents
- `agents/vmaw.py` — VMAW fact-checker agent

#### Config
- `config/prompts/vmaw.j2` — VMAW system prompt

#### Pipeline composition
- `pipeline/graph_v3.py` — frozen Phase 3 LangGraph (Phase 2 graph + VMAW invocation from Arbiters + re-planning loop)

#### UI (Phase 3, frozen after Phase 3)
- `ui/phase3/app.py` — Streamlit shell
- `ui/phase3/results_browser.py` — lists docs from BigQuery `runs_v3`
- `ui/phase3/pages/overview.py`
- `ui/phase3/pages/vmaw_traces_view.py` — when an Arbiter invoked VMAW, show the trace: conflict signal, evidence VMAW gathered, verdict, corrected value
- `ui/phase3/pages/sme_review_for_ambiguous.py` — for ambiguous docs: list of flagged items, click any flag → PDF viewer scrolls to and highlights the exact bbox
- `ui/phase3/components/pdf_chunk_highlighter.py` — new component, renders PDF page as image with bbox overlay drawn at the cited DocAI coordinates
- `ui/phase3/components/trace_viewer.py` — agent trace renderer (Thought → Action → Observation steps)

#### Project-level
- `MANIFEST_PHASE_3.md` — live during Phase 3 development

### Files modified in Phase 3
- `config/storage.yaml` — append `phase_3:` block
- `pipeline/runner.py` — add `v3` route
- `requirements.txt` — append `pdf2image`, `Pillow` if not already present

### Phase 3 UI demonstrates
- Same browsing experience
- VMAW Traces tab → for each VMAW invocation, see: which Arbiter called it, the conflict signal, VMAW's tool calls (locate, expand context, cite evidence), the verdict (EVIDENCE_FOUND / RESOLVED / CONFIRMED / UNRESOLVABLE), the corrected value
- SME Review tab → for ambiguous docs, list flagged items with: which team flagged, which field, why. Click any flag → PDF viewer **navigates to the exact page and scrolls/zooms to the bbox** with a colored overlay highlighting the precise region. SME sees the visual evidence + the text excerpt + the agent's reasoning side-by-side
- Re-planning loop visible: when verifiers flagged Fixable, see the Planner's second pass

### Phase 3 acceptance criteria
- `demo.pdf` + mock fixtures processed via `process_local.py --version v3` → BigQuery `extractions_v3`
- VMAW invocations correctly serialized to GCS artifacts (full ReAct trace stored)
- bbox-highlighting PDF viewer works on real DocAI coordinates from cached layout
- SME review experience tested: click a flag → see the right region of the right page

### End of Phase 3
- `scripts/snapshot_phase.py --phase 3`
- Finalize `MANIFEST_PHASE_3.md`
- Git tag `v3.0-phase3`
- Demo to business

---

# Phase 4 — Full SME Review + learning loop + nightly eval

## Goal
Complete the human-in-the-loop story. Full SME review UI with
approve/edit/reject controls. SME corrections feed back into Vertex AI
Vector Search as few-shot examples (learning loop). A standalone eval
script that runs against the labeled ground-truth set and writes a
regression report.

**Scope clarification (2026-05-18):** Phase 4 is **dev-environment only**.
No Dataflow. No Terraform. No production-deploy automation. GCP resources
remain manually provisioned. When the system eventually goes to production,
the ops/platform team handles deployment in their own way — that's out of
scope for this project.

## What we build

### Files added in Phase 4

#### Learning loop
- `learning/__init__.py`
- `learning/vector_search.py` — Vertex AI Vector Search client (read + write)
- `learning/few_shot_retriever.py` — retrieves similar past corrections at extraction time (injected into Extractor prompts)
- `learning/sme_correction_writer.py` — when an SME approves/edits a flagged item, writes the correction as a new few-shot example

#### Pipeline composition
- `pipeline/graph_v4.py` — frozen Phase 4 LangGraph (Phase 3 graph + SME interrupt node + Vector Search context injection in extractors)

#### Scripts
- `scripts/eval_pipeline.py` — runs the pipeline across every PDF in `ground_truth/` and writes a per-field regression report. Manual invocation only (no cron, no Dataflow).

#### UI (Phase 4, frozen after Phase 4)
- `ui/phase4/app.py`
- `ui/phase4/results_browser.py`
- `ui/phase4/pages/overview.py`
- `ui/phase4/pages/sme_queue.py` — queue of `sme_flag` docs. SME picks one.
- `ui/phase4/pages/full_sme_review.py` — full SME workflow: PDF with all flagged regions overlaid → approve / edit / reject controls per field → submit resumes the LangGraph run
- `ui/phase4/pages/learning_loop_view.py` — recent SME corrections being indexed into Vector Search, with similarity scores for future docs
- `ui/phase4/components/sme_review_form.py` — approve/edit/reject form with audit fields

#### Project-level
- `MANIFEST_PHASE_4.md` — live during Phase 4 development

### Files modified in Phase 4
- `config/storage.yaml` — append `phase_4:` block + Vector Search endpoint config
- `pipeline/runner.py` — add `v4` route

### Phase 4 UI demonstrates
- SME Queue → list of flagged docs with reasons.
- Full SME Review → PDF with flagged regions overlaid → form to approve/edit/reject each → submit → LangGraph run resumes (Firestore checkpoint loaded back) → corrected output written to BigQuery.
- Learning Loop View → most recent SME corrections indexed into Vector Search → similarity scores showing which future documents would benefit from these corrections as few-shots.

### Phase 4 acceptance criteria
- SME interrupt → resume cycle works (LangGraph state survives the human-in-the-loop pause via Firestore checkpointer)
- SME corrections written to Vector Search
- Next document processed retrieves prior corrections as few-shot context for its Extractor calls
- `eval_pipeline.py` runs to completion against the labeled ground-truth set and emits a regression report (per-field pass rate, deltas vs the prior eval)

### End of Phase 4
- `scripts/snapshot_phase.py --phase 4`
- Finalize `MANIFEST_PHASE_4.md`
- Git tag `v4.0-phase4`
- Demo to business
- **All 4 phases complete. Ready for transfer to production codebase using the 4 manifests as Option-A commits.**

---

# Final transfer to production codebase (after Phase 4)

1. Open `MANIFEST_PHASE_1.md`, copy listed files into production codebase, commit as `Phase 1 — Foundation + MetadataTeam end-to-end`
2. Open `MANIFEST_PHASE_2.md`, copy listed files (new + the documented YAML deltas), commit as `Phase 2 — All 4 Specialist Teams + Planner + Linking`
3. Open `MANIFEST_PHASE_3.md`, copy listed files, commit as `Phase 3 — VMAW + chunk-level SME review`
4. Open `MANIFEST_PHASE_4.md`, copy listed files, commit as `Phase 4 — Full SME Review + learning loop + production deploy`

Result: 4 clean phase commits in production codebase, each independently reviewable, each shipping working capability.

---

# Manifest discipline (live throughout development)

Every Phase N MANIFEST file is updated **every time we touch a file in that phase's scope**. Three event types:

- **ADD** — new file created in Phase N → log path
- **MODIFY** — Phase N file changed during Phase N development → log path + brief summary of change
- **MODIFY (config)** — cross-phase config file (e.g. `storage.yaml`) modified by Phase N → log path + the lines added

The manifest at the end of Phase N is the complete answer to "what does Phase N contribute?" — used directly when transferring to production codebase.

If we revisit Phase 1 during Phase 3 work to fix a bug, the change goes into `MANIFEST_PHASE_1.md` as a MODIFY (not into Phase 3's manifest) because logically it's a Phase 1 file. This preserves clean phase boundaries in the final commits.
