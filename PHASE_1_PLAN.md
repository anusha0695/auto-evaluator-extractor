# Phase 1 — Implementation Plan

**Goal:** End-to-end working pipeline. Read a PDF from GCS → preprocess (DocAI Layout Parser + Block Profiler + SciSpaCy Medical NER) → **MetadataTeam** extracts patient + provider + practice info per schema-v2 `report_metadata` → schema validate → auto-accept or SME flag → persist to BigQuery + GCS → display in Streamlit UI.

**Estimated effort:** 10 working days, 1 senior backend engineer (compresses to 7 with 2 engineers in parallel after Block B is done).

**Primary validation doc:** `data/actual_docs/demo.pdf` — a real NeoGenomics fax (William Dawson, JAK2 V617F Quantitative). Ground truth at `ground_truth/demo.json` covers **all four umbrella sections** of schema v2 (Phase 1 only scores `report_metadata`; the others are there for Phase 2/3 regression).

**Secondary regression fixtures:** 5 synthetic mock PDFs (`doc_3.pdf`, `doc_11.pdf`, `doc2_1.pdf`, `doc2_6.pdf`, `doc2_25.pdf`) from two distinct templates (Tempus xT and unknown_vendor_v2). Ground truth at `ground_truth/phase1/*.json` covers only `report_metadata`.

**End-state demo:** A processed PDF browsable in the Phase 1 Streamlit app, showing preprocessing visualizations and the extracted `report_metadata` JSON with page citations.

---

## Prerequisites (must be in place before coding starts)

These happen in parallel with Block A. Mostly infrastructure provisioning that should be raised as separate tickets.

### Infrastructure
- [ ] GCP project `medical-report-extraction` with billing enabled
- [ ] DocAI processor of type `LAYOUT_PARSER_PROCESSOR` in `us`, pinned to version `pretrained-layout-parser-v1.6-2026-01-13` (processor ID `81e83f6783d90bb0`)
- [ ] BigQuery dataset created (e.g. `extractor_dev`) with tables `extractions_v1` + `runs_v1`
- [ ] GCS bucket `gs://patient_clinical_trial/` with prefixes:
  - `gs://patient_clinical_trial/patient_profiles/` — source PDFs
  - `gs://patient_clinical_trial/extraction_outputs/phase1/` — raw DocAI responses, agent traces, audit artifacts
- [ ] Firestore database (Native mode) in the same project — used as the LangGraph checkpointer
- [ ] Service account `pipeline-preprocess@medical-report-extraction.iam.gserviceaccount.com` with roles:
  - `roles/documentai.apiUser`
  - `roles/aiplatform.user`
  - `roles/storage.objectViewer` on the input prefix
  - `roles/storage.objectAdmin` on the artifacts prefix
  - `roles/bigquery.dataEditor` on the dataset
  - `roles/datastore.user` (Firestore checkpointer reads/writes)
- [ ] **BAA confirmation** from compliance team that DocAI + Vertex AI Gemini cover PHI processing
- [ ] Cloud Trace + Cloud Logging + Cloud Monitoring APIs enabled

### Medical NER (in-process, no separate endpoint)
- [ ] `scispacy` installed via pip in the project venv
- [ ] SciSpaCy models downloaded as Python packages:
  - `en_ner_bionlp13cg_md` — cancer/gene/anatomy entities
  - `en_ner_bc5cdr_md` — chemicals + diseases
- [ ] `en_core_web_sm` (general English NER for PERSON/DATE/ORG/GPE)
- [ ] `medspacy` for negation detection (used Phase 2+, installed now for forward compat)

No Vertex AI Custom Prediction endpoint needed for NER — the SciSpaCy models run locally in the same process as the pipeline. PHI never leaves the worker.

### Data
- [x] `data/actual_docs/demo.pdf` uploaded — real NeoGenomics fax (William Dawson, JAK2 V617F Quantitative)
- [x] `ground_truth/demo.json` — full 4-section ground truth for demo.pdf (schema v2)
- [x] `data/input/raw_documents/doc_*.pdf` + `doc2_*.pdf` — 16 synthetic mock PDFs available; 5 selected as Phase 1 fixtures (doc_3, doc_11, doc2_1, doc2_6, doc2_25)
- [x] `ground_truth/phase1/*.json` — 5 mock ground truths in schema v2 (report_metadata only)
- [ ] Sample PDFs uploaded to `gs://patient_clinical_trial/patient_profiles/` so the pipeline can read them through the GCS client (mirror of the local `data/` tree)

### Development environment
- [ ] Python 3.11
- [ ] `gcloud` CLI authenticated to project `medical-report-extraction`
- [ ] Application Default Credentials configured
- [ ] `poppler-utils` installed for the Streamlit PDF viewer (`brew install poppler` on macOS dev boxes)

---

## Work blocks (in execution order)

### Block A — Project scaffolding  *(½ day)*

**Goal:** establish the project skeleton.

**Files to create:**
- `pyproject.toml` — project metadata + dependencies
- `requirements.txt` — pinned versions
- `.env.example` — template for required env vars (project, locations, processor IDs, bucket name, BigQuery dataset, Firestore database name, feature flags, model temperature 0.0)
- `.gitignore`
- `README.md` — Phase 1 setup + run instructions
- `MANIFEST_PHASE_1.md` — header initialized; gets updated continuously throughout Phase 1

**Dependencies for Phase 1:**
- `langgraph`, `langchain-core`, `langchain-google-vertexai`
- `google-cloud-documentai`, `google-cloud-storage`, `google-cloud-bigquery`, `google-cloud-firestore`
- `google-cloud-aiplatform` (Vertex AI Gemini)
- `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-gcp-trace`
- `pydantic>=2.0`
- `jsonschema`
- `jinja2`
- `pyyaml`
- `structlog`
- `tenacity`
- `streamlit`
- `pdf2image`, `Pillow` (for the PDF viewer)
- `scispacy`, `en-ner-bionlp13cg-md`, `en-ner-bc5cdr-md`, `en-core-web-sm` (Medical NER — in-process)
- `medspacy` (installed now, actively used Phase 2+)
- `hgvs` (installed now, actively used Phase 2+)
- `pytest`, `pytest-asyncio`, `mypy` (dev)

---

### Block B — Config files  *(½ day)*

**Goal:** lay down the source-of-truth schema and config; these drive everything downstream.

**Files to create:**
- `config/schemas/genomic_pathology_v2.json` — full v2 JSON Schema covering all 4 umbrella sections: `report_metadata`, `Genomic_Variant_umbrella`, `other_molecular_biomarker_umbrella`, `tested_biomarker_umbrella`. **Already created** as part of the pre-Block-A batch.
- `config/storage.yaml` — `phase_1` entry mapping to BigQuery tables `extractions_v1` + `runs_v1` and GCS prefix `gs://patient_clinical_trial/extraction_outputs/phase1/`
- `config/teams.yaml` — `metadata_team` registered, mapping to the `report_metadata` schema section
- `config/tools.yaml` — initial tool registry (pdf_page_loader, pdf_text_search, docai_layout_lookup, npi_validator, date_parser, schema_validate, state_read)
- `config/prompts/system/extractor.j2` — base ReAct extractor system prompt template
- `config/prompts/system/coverage_auditor.j2` — base auditor template
- `config/prompts/system/arbiter.j2` — base arbiter template
- `config/prompts/metadata_team.j2` — metadata-specific extraction prompt (covers patient identity, vendor, dates, ordering provider, practice; encodes the fax-header-filtering rule, vendor-inference rule, practice-vs-vendor disambiguation rule, MRN strict labeling rule)
- `config/prompts/preprocess/block_profiler.j2` — Block Profiler system prompt
- `config/prompts/preprocess/medical_ner.j2` — Medical NER prompt (parser-hypothesis adapter)

**Key decision:** prompt templates reference schema sections by name. The Jinja renderer pulls field descriptions from the JSON Schema at render time. Same `extractor.j2` template works for any team, parameterized by the team's schema section.

---

### Block C — Core engine  *(1 day)*

**Goal:** the schema-independent foundation that every later module imports.

**Files to create (these get frozen after Phase 1):**
- `core/__init__.py`
- `core/state.py` — `PipelineState` TypedDict (the LangGraph state object). Keys include `doc_id`, `pipeline_version`, `gcs_uri`, `raw_pdf_bytes`, `doc_profile`, `parser_hypothesis`, `team_outputs`, `verifier_scorecards`, `verdict`, `preprocessing_errors`.
- `core/errors.py` — typed `PreprocessingError` family with `doc_id`, `retry_safe` flags.
- `core/schema_loader.py` — class `SchemaLoader` that loads `genomic_pathology_v2.json`, exposes a method to get a Pydantic model for a schema section (uses `pydantic.create_model` under the hood with type mapping from JSON Schema), exposes a method to get the field metadata (`description`, `extraction_mode`) for prompt rendering.
- `core/prompt_renderer.py` — class `PromptRenderer` that loads Jinja templates, injects schema-section context (field names + descriptions + VERBATIM/DERIVED tags from the loaded schema), renders the final prompt string.
- `core/gcs_client.py` — async wrapper around `google.cloud.storage` for listing PDFs in a bucket prefix and reading PDF bytes.
- `core/persistence.py` — class `Persistence` that reads `config/storage.yaml`, writes pipeline runs to the configured BigQuery `runs_v1` table, writes extractions to the configured `extractions_v1` table, writes raw artifacts to GCS under `{prefix}/{doc_id}/`. All writes tagged with `pipeline_version`.
- `core/observability.py` — OpenTelemetry SDK setup: trace provider, GCP trace exporter, span helpers (`@trace` decorator and `span()` context manager). Wraps every node + tool call automatically.
- `core/checkpointer.py` — thin factory returning a Firestore-backed LangGraph checkpointer.
- `core/tool_registry.py` — registers all shared tools as LangChain `@tool` decorated functions with Pydantic input schemas.

**Key decision:** `schema_loader` generates Pydantic models *dynamically* from JSON Schema, so the schema file is the only source of truth for field definitions. When the schema changes, nothing else needs to change in code.

---

### Block D — Preprocessing layer  *(1 day)*

**Goal:** the three preprocessing components that every downstream agent reads from.

**Files to create:**
- `preprocess/__init__.py`
- `preprocess/docai_parser.py` — `DocAIParser` class wrapping the Layout Parser processor. Method `parse(gcs_uri) -> tuple[DocProfile, str]` returns the parsed profile (sections, tables, every block with bbox + section_path, per-page text) plus the GCS URI where the raw response is cached.
- `preprocess/fax_header_filter.py` — deterministic stripper that drops fax-transport bands ("** INBOUND NOTIFICATION : FAX RECEIVED SUCCESSFULLY **", "TIME RECEIVED ... REMOTE CSID ... DURATION ... PAGES ... STATUS Received", and "MM.DD.YYYY HH:MM:SS Page: N of M Sender: ... Recipient: ...") from per-page text before downstream agents see it. Driven by `demo.pdf`'s real-world quirks.
- `preprocess/block_profiler.py` — `BlockProfiler` class. Method `profile(blocks: list[BlockInfo]) -> list[BlockProfile]`. Single Gemini 2.5 Flash call per document at temperature 0.0, structured output mode using the BlockProfile Pydantic schema. Reads the `block_profiler.j2` prompt. **Treats fax-header blocks as `text_role: fax_transport_noise` and excludes them from downstream extraction scopes.**
- `preprocess/medical_ner.py` — `SciSpaCyMedicalNER` class. Loads `en_ner_bionlp13cg_md`, `en_ner_bc5cdr_md`, `en_core_web_sm` (and `medspacy` for Phase 2+) **in-process** as Python packages. Returns `ParserHypothesis` (independent entity candidates with offsets, label, source model). Models are loaded once at module import and reused across documents.
- `preprocess/preprocess_node.py` — LangGraph node that runs DocAI sequentially (Profiler needs its blocks), then runs Fax Header Filter, then runs Block Profiler + Medical NER in parallel via `asyncio.gather`. All-or-nothing on failure.

**Key decision:** Medical NER runs **in-process** via the SciSpaCy pip packages. No separate Vertex Custom Prediction endpoint to provision, deploy, or pay for. The same pipeline binary that runs the LangGraph also calls `nlp(page_text)`.

**Key decision:** Block Profiler classifies *every* block from DocAI (page-headers, footers, captions, content, fax-transport-noise, etc.), not just RAG chunks. This gives the Planner (Phase 2) a complete map of what each block is and lets us blacklist fax-header regions cleanly.

---

### Block E — Agents (Phase 1 set)  *(1 day)*

**Goal:** abstract Agent base + the three agent roles every team uses.

**Files to create:**
- `agents/__init__.py`
- `agents/base.py` — abstract `Agent` class. Holds: agent_id, model name, system prompt template path, tool list. Method `invoke(state, scope) -> AgentResult`. Wraps every call in an OpenTelemetry span. All Gemini calls fixed at `temperature=0.0`.
- `agents/extractor.py` — `Extractor(Agent)` with ReAct loop. Uses Gemini 2.5 Pro with tool calling. Loads the team-specific prompt from config (e.g. `metadata_team.j2`) + the base `extractor.j2` system prompt. Emits structured output validated against the team's Pydantic schema section.
- `agents/coverage_auditor.py` — `CoverageAuditor(Agent)`. Different prompt (`coverage_auditor.j2`) that asks "what did the Extractor miss?". Cross-references against `parser_hypothesis` count. Returns `coverage_ok` or `gap_signal`.
- `agents/arbiter.py` — `Arbiter(Agent)` using Gemini 2.5 Flash. Only fires on conflict. Picks one of three policies: `ACCEPT_EXTRACTOR`, `RE_EXTRACT`, or `INVOKE_VMAW` (Phase 1 returns SME flag for the VMAW case since VMAW ships in Phase 3).

**Key decision:** agents are *configurable*, not hardcoded per team. The same `Extractor` class is instantiated four times across the system (once per team) with different prompt templates and different schema sections. MetadataTeam in Phase 1 uses the same Extractor class that GenomicVariantTeam / MolecularBiomarkerTeam / TestedBiomarkerTeam will use in Phase 2.

---

### Block F — MetadataTeam  *(½ day)*

**Goal:** wire the three agents into the MetadataTeam subgraph.

**Files to create:**
- `teams/__init__.py`
- `teams/metadata_team.py` — `MetadataTeam` class. Composes Extractor → Coverage Auditor → conditional Arbiter (only invoked on gap_signal or low confidence). Returns committed `metadata_entities` as a Pydantic instance of the `report_metadata` schema section.

**Key decision:** team subgraph is built as a LangGraph subgraph so it can run with its own state + Firestore checkpointing, and can be re-invoked independently.

---

### Block G — Verification + Decision  *(½ day)*

**Goal:** validate the team output and route to either auto-accept or SME flag.

**Files to create:**
- `verification/__init__.py`
- `verification/schema_validator.py` — runs the Pydantic model (generated by `schema_loader` from `genomic_pathology_v2.json`) against the team output. Returns pass/fail with field-level errors. Pure deterministic, no LLM.
- `decision/__init__.py`
- `decision/decision_router.py` — given verifier scorecards + the team's confidence, routes to `auto_accept` or `sme_flag`. Phase 1 has only schema_validator; routing is simple: if validator passes and `report_metadata.llm_confidence_score > 0.85`, auto-accept; otherwise SME flag.

---

### Block H — Pipeline graph + runner  *(½ day)*

**Goal:** compose the LangGraph for Phase 1 and provide the entry point.

**Files to create:**
- `pipeline/__init__.py`
- `pipeline/graph_v1.py` — frozen Phase 1 LangGraph composition. Builds a `StateGraph` with nodes: `document_received` → `preprocess_node` → `metadata_team_node` → `schema_validator_node` → `decision_router_node` → `persist_node` → `END`. Wired to the Firestore checkpointer. This file is **frozen** after Phase 1 ships.
- `pipeline/runner.py` — entry point. Function `run(doc_id: str, gcs_uri: str, version: str = "v1") -> RunResult`. Dispatches to the right graph by version. Wraps the whole run in an OpenTelemetry trace.

---

### Block I — Local scripts  *(½ day)*

**Goal:** the CLI tools to actually run the pipeline.

**Files to create:**
- `scripts/__init__.py`
- `scripts/process_local.py` — CLI script. Args: `--gcs-uri` or `--list-bucket gs://patient_clinical_trial/patient_profiles/` (process all PDFs in a bucket prefix), `--version v1`. Invokes `runner.run()` and prints summary to stdout. This is the primary dev tool.
- `scripts/snapshot_phase.py` — CLI script. Args: `--phase 1`. Copies `ui/phase1/` and `pipeline/graph_v1.py` into `snapshots/phase1/`. Used at end of phase.

---

### Block J — Initial end-to-end validation  *(1 day)*

**Goal:** prove the pipeline works on the primary doc before building the UI.

**Activities:**
- Upload `data/actual_docs/demo.pdf` to `gs://patient_clinical_trial/patient_profiles/demo.pdf`
- Run `python scripts/process_local.py --gcs-uri gs://patient_clinical_trial/patient_profiles/demo.pdf --version v1`
- Verify:
  - DocAI raw response cached to `gs://patient_clinical_trial/extraction_outputs/phase1/demo/docai-raw.json`
  - `block_profiles.json` cached (fax-header band classified as `fax_transport_noise`)
  - `parser_hypothesis.json` cached (SciSpaCy entities serialized)
  - MetadataTeam output written to BigQuery `extractions_v1`
  - Run metadata written to BigQuery `runs_v1`
  - OpenTelemetry trace visible in Cloud Trace
  - LangGraph checkpoint visible in Firestore
- Compare extracted `report_metadata` against `ground_truth/demo.json` `genomic_pathology_extraction.report_metadata` field-by-field
  - Must pass on Vendor_Name='NeoGenomics', Practice_Name='Hematology & Oncology Consultants', Patient_MRN=null, all 3 dates ISO-normalized
- Repeat for all 5 mock fixtures from `ground_truth/phase1/`
- Fix any issues uncovered. Add unit tests for components that break.

**Acceptance gate:** if this block fails, we don't proceed to UI work. Pipeline correctness comes first.

---

### Block K — Streamlit UI for Phase 1  *(2 days)*

**Goal:** Phase 1 UI that browses already-processed docs and shows preprocessing + metadata extraction.

**Files to create (all under `ui/phase1/`, frozen after Phase 1):**
- `ui/__init__.py`
- `ui/phase1/__init__.py`
- `ui/phase1/app.py` — Streamlit main shell. Sidebar shows results_browser; main area routes to selected page.
- `ui/phase1/results_browser.py` — queries BigQuery `runs_v1` for all processed docs, displays as a sortable table (doc_id, processed_at, verdict, cost, latency). User clicks a row to select.
- `ui/phase1/pages/__init__.py`
- `ui/phase1/pages/overview.py` — shows the selected doc's metadata, the run summary (verdict, cost, latency, model versions used).
- `ui/phase1/pages/preprocessing_view.py` — visualizes DocAI layout blocks on the PDF (color-coded by `text_role` from the Block Profiler — including the `fax_transport_noise` class so SMEs can see what was filtered), shows Medical NER candidates highlighted in the per-page text.
- `ui/phase1/pages/metadata_extraction_view.py` — extracted `report_metadata` JSON in a tree view; clicking any field jumps the PDF viewer to its source page. Special widget for `Patient_MRN=null` showing the strict-MRN rule the system applied.
- `ui/phase1/components/__init__.py`
- `ui/phase1/components/pdf_viewer.py` — page-level PDF viewer using `pdf2image` to render pages as images.
- `ui/phase1/components/json_tree.py` — recursive collapsible JSON tree renderer with page-citation hyperlinks.

**Run with:** `streamlit run ui/phase1/app.py` → opens at `http://localhost:8501`.

---

### Block L — Demo prep, snapshot, manifest, git tag  *(½ day)*

**Goal:** ship Phase 1.

**Activities:**
- Run `process_local.py --list-bucket gs://patient_clinical_trial/patient_profiles/ --version v1` to process demo.pdf + the 5 mock fixtures
- Verify all 6 appear in the Phase 1 Streamlit app
- Walk through the UI as a dry run of the business demo (lead with demo.pdf — it's the real-world story)
- Run `python scripts/snapshot_phase.py --phase 1` → `snapshots/phase1/` populated
- Finalize `MANIFEST_PHASE_1.md` — every file added, every file modified, every config delta
- Git tag `v1.0-phase1`
- Demo to business team

---

## Acceptance criteria for Phase 1 complete

1. Pipeline processes `demo.pdf` end-to-end via `process_local.py --version v1`
2. DocAI Layout Parser response, fax-header-filtered text, Block Profiler output, and SciSpaCy parser-hypothesis all cached in GCS under `gs://patient_clinical_trial/extraction_outputs/phase1/demo/`
3. MetadataTeam extracts `report_metadata` for `demo.pdf` matching `ground_truth/demo.json` on **≥ 80% of fields** (acceptance gate)
4. The 5 mock fixtures (`doc_3`, `doc_11`, `doc2_1`, `doc2_6`, `doc2_25`) also pass ≥ 80% on `report_metadata`
5. Output written to BigQuery `extractions_v1` and `runs_v1` with `pipeline_version=v1`
6. OpenTelemetry traces appear in Cloud Trace, tagged by `doc_id` and `pipeline_version`
7. LangGraph checkpoints visible in Firestore
8. Phase 1 Streamlit app runs at `streamlit run ui/phase1/app.py`, lists all 6 processed docs, displays preprocessing visualizations + `report_metadata` JSON for each
9. `snapshots/phase1/` populated with frozen UI + graph_v1.py
10. `MANIFEST_PHASE_1.md` lists every file in Phase 1's contribution
11. Git tag `v1.0-phase1` applied

---

## Suggested 10-day schedule

| Day | Work |
|---|---|
| 1 | Block A (scaffolding) + Block B (config — schema-v2 already exists from pre-Block-A batch) |
| 2 | Block C (core engine — state, errors, schema_loader, prompt_renderer) |
| 3 | Block C continued (gcs_client, persistence, observability, checkpointer, tool_registry) |
| 4 | Block D (preprocessing — DocAI, fax_header_filter, Block Profiler) |
| 5 | Block D continued (in-process SciSpaCy Medical NER, preprocess_node) + Block E start (agents base + extractor) |
| 6 | Block E continued (coverage_auditor, arbiter) + Block F (MetadataTeam) + Block G (verifier + decision) |
| 7 | Block H (graph_v1, runner) + Block I (process_local.py, snapshot_phase.py) + Block J (e2e validation on demo.pdf) |
| 8 | Block K (UI — app, results_browser, overview, preprocessing_view) |
| 9 | Block K continued (metadata_extraction_view, pdf_viewer, json_tree) + dry run on demo.pdf + 5 fixtures |
| 10 | Block L (snapshot, manifest, tag, demo) |

Compresses to 7 days with 2 engineers in parallel (one on agents+teams, one on UI) starting from Day 5.

---

## Key technical decisions locked for Phase 1

1. **Schema:** `config/schemas/genomic_pathology_v2.json` is the single source of truth. Drives Pydantic generation **and** Gemini structured output spec.
2. **Prompts as Jinja templates** that pull schema-section context (field names, descriptions, `extraction_mode` VERBATIM/DERIVED tags) from the loaded schema.
3. **OpenTelemetry → Cloud Trace + Cloud Logging + Cloud Monitoring** for observability. No LangSmith — PHI stays in our GCP project.
4. **LangGraph state checkpointer:** **Firestore** (in the same GCP project, BAA-covered).
5. **Medical NER:** **in-process SciSpaCy** (`en_ner_bionlp13cg_md` + `en_ner_bc5cdr_md` + `en_core_web_sm`). No separate Vertex endpoint. Phase 2 will additionally activate MedSpaCy negation + HGVS + HGNC alias lookup — installed now for forward compat, used later.
6. **DocAI Layout Parser** for structural parsing (no Custom Extractor — that's the team's job, not preprocessing's). Processor pinned to version `pretrained-layout-parser-v1.6-2026-01-13`.
7. **Fax-header filter** runs before Block Profiler, deterministic regex; Block Profiler also labels surviving fax-noise blocks as `fax_transport_noise`.
8. **Gemini 2.5 Flash** for Block Profiler (cheap classification) at **temperature 0.0**.
9. **Gemini 2.5 Pro** for Extractor + Coverage Auditor; **Gemini 2.5 Flash** for Arbiter (cascading). All at **temperature 0.0**.
10. **All-or-nothing preprocessing** — partial failures route to SME, never proceed with incomplete state.
11. **Read-only Streamlit UI** — never triggers processing, only browses persisted results.
12. **GCS file browser** in UI for input doc selection — no file upload from the UI.
13. **Always-emit-all-4-umbrella-sections** rule applied — even in Phase 1 the MetadataTeam output is wrapped in the full schema-v2 envelope with empty `Genomic_Variant_umbrella` / `other_molecular_biomarker_umbrella` / `tested_biomarker_umbrella` placeholders. This means Phase 1 output is forward-compatible with Phase 2+ schema validation.

---

## Manifest discipline reminder

`MANIFEST_PHASE_1.md` is updated *every time we touch a file in the Phase 1 scope*. Three event types:

- **ADD** — new file → log path
- **MODIFY** — Phase 1 file changed during Phase 1 development → log path + summary of change
- **MODIFY (config)** — `storage.yaml`, `teams.yaml`, `requirements.txt`, `README.md` modified → log path + the lines added

The manifest at the end of Phase 1 is the source of truth for the Phase 1 commit during final transfer.
