# MANIFEST — Phase 1

Live shipping manifest for the Phase 1 commit. Every ADD / MODIFY of a file in Phase 1's scope is logged here as it happens. At end-of-phase this is the source of truth for the Phase 1 commit in the production codebase.

**Phase 1 scope:** Foundation + MetadataTeam end-to-end. Preprocessing (DocAI + fax filter + Block Profiler + in-process SciSpaCy) → MetadataTeam → schema validator → decision router → persist → Streamlit results browser.

**Schema:** `config/schemas/genomic_pathology_v2.json` (all 4 umbrella sections, MetadataTeam populates `report_metadata`).

**Primary validation:** `data/actual_docs/demo.pdf` (NeoGenomics JAK2 V617F Quantitative, William Dawson).

**Acceptance gate:** ≥ 80% per-field match on `report_metadata` vs `ground_truth/demo.json` and `ground_truth/phase1/*.json`.

---

## Event log

Format: `[YYYY-MM-DD] EVENT path/to/file — short note`

### Pre-Block-A batch — schema + ground truth + plan updates (2026-05-17)

- [2026-05-17] ADD `config/schemas/genomic_pathology_v2.json` — full v2 JSON Schema covering all 4 umbrella sections (`report_metadata`, `Genomic_Variant_umbrella`, `other_molecular_biomarker_umbrella`, `tested_biomarker_umbrella`), per-field `extraction_mode` annotations (VERBATIM / DERIVED / VERBATIM_OR_INFERRED), `additionalProperties: false` on every object.
- [2026-05-17] ADD `ground_truth/demo.json` — full 4-section ground truth for `data/actual_docs/demo.pdf` (NeoGenomics JAK2 V617F Not Detected, William Dawson, 2 pages). Includes 15 manual-review notes covering fax-header noise, vendor evidence chain, practice-vs-vendor disambiguation, blank-MRN rule, ISO date normalization.
- [2026-05-17] MODIFY `ground_truth/phase1/doc_3.json` — migrated to schema v2 format. Tempus xT mock, vendor inferred from "xT" product marker, no MRN, `phase1_scoring_scope: "report_metadata only"`.
- [2026-05-17] MODIFY `ground_truth/phase1/doc_11.json` — migrated to schema v2 format. Tempus xT mock (Marilyn Mcbride, Lung Sample).
- [2026-05-17] MODIFY `ground_truth/phase1/doc2_1.json` — migrated to schema v2 format. unknown_vendor_v2 mock, physician extracted from page footer.
- [2026-05-17] MODIFY `ground_truth/phase1/doc2_6.json` — migrated to schema v2 format. unknown_vendor_v2 mock (Robert Johnson, header diagnosis vs page-4 diagnosis).
- [2026-05-17] MODIFY `ground_truth/phase1/doc2_25.json` — migrated to schema v2 format. unknown_vendor_v2 mock (Roberto Adkins, same-day Collection / Test Initiated).
- [2026-05-17] MODIFY `ground_truth/phase1/README.md` — replaced with v2 version. Distinguishes primary (`demo.json`, all 4 sections) from secondary (5 mock fixtures, `report_metadata` only). Documents 3 doc templates and 9 prompt-design implications.
- [2026-05-17] MODIFY `PHASE_1_PLAN.md` — rewritten for schema v2. AdminTeam → MetadataTeam rename. `pathology_v3` → `genomic_pathology_v2`. `administrative_info` → `report_metadata`. In-process SciSpaCy (no Vertex Custom Prediction endpoint). `demo.pdf` as primary acceptance doc. GCS bucket `patient_clinical_trial` with `patient_profiles/` + `extraction_outputs/phase1/`. Added Block D file `preprocess/fax_header_filter.py`. Firestore checkpointer added.
- [2026-05-17] MODIFY `PHASES.md` — rewritten with same renames across all 4 phases. New schema-section ↔ implementation-phase disambiguation. `medical-report-extraction` as GCP project everywhere.

### Block A — Project scaffolding (2026-05-17)

- [2026-05-17] ADD `pyproject.toml` — PEP 621 metadata + runtime dependencies pinned to compatible ranges. SciSpaCy + spaCy + MedSpaCy + HGVS all listed. Python 3.11 only. Dev extras (`pytest`, `mypy`, `ruff`).
- [2026-05-17] ADD `requirements.txt` — full lockfile with SciSpaCy + spaCy model URLs (direct downloads from allenai S3 and explosion GitHub releases). Comment-documented install steps.
- [2026-05-17] ADD `.env.example` — template covering GCP project, DocAI processor IDs, Vertex Gemini settings (`GEMINI_TEMPERATURE=0.0` locked), GCS bucket layout, BigQuery tables, Firestore collection, OpenTelemetry settings, feature flags.
- [2026-05-17] ADD `.gitignore` — Python + venv + IDE + macOS, plus explicit denylist for `.env*`, service-account JSON files, `local_runs/`, `local_artifacts/`, `scratch/`, and any cached DocAI responses to prevent accidental PHI commits. `ground_truth/` and `snapshots/` explicitly allowed.
- [2026-05-17] ADD `README.md` — Phase 1 setup + run instructions, repo layout, install / run / browse / acceptance gate sections, PHI handling explainer.
- [2026-05-17] ADD `MANIFEST_PHASE_1.md` — this file. Initialized with Pre-Block-A + Block A entries.

### Block B — Config files (2026-05-17)

- [2026-05-17] ADD `config/storage.yaml` — `phase_1` entry: bucket `patient_clinical_trial` with `extraction_outputs/phase1/` prefix, BigQuery `medical-report-extraction.extractor_dev.{runs_v1, extractions_v1}`, Firestore checkpoint collection `langgraph_checkpoints_phase1`, input prefix `patient_profiles/`. Stub comments for Phase 2-4 entries.
- [2026-05-17] ADD `config/teams.yaml` — `metadata_team` registered → schema section `report_metadata`. Model assignments: Extractor + Coverage Auditor = `gemini-2.5-pro`, Arbiter = `gemini-2.5-flash`, **all at temperature 0.0** (locked). Tool allowlist of 7 tools. `auto_accept_confidence_threshold: 0.85`, `coverage_gap_tolerance: 0.10`. Schema path lives at top-level.
- [2026-05-17] ADD `config/tools.yaml` — 7-tool registry: `pdf_page_loader`, `pdf_text_search`, `docai_layout_lookup`, `npi_validator`, `date_parser`, `schema_validate`, `state_read`. Each entry: description (used by prompt renderer), handler import path (used by `core/tool_registry.py`), and an `input_schema` block that becomes the LangChain tool's Pydantic input model.
- [2026-05-17] ADD `config/prompts/system/extractor.j2` — base ReAct extractor system prompt, schema-driven via Jinja context (team_name, schema_section_name, fields with VERBATIM/DERIVED tags, available_tools, parser_hypothesis_count). Encodes the always-emit-all-4-umbrellas invariant, the ` | ` concatenation rule, the never-fabricate rule, and the ReAct loop format. 108 lines after render.
- [2026-05-17] ADD `config/prompts/system/coverage_auditor.j2` — base auditor prompt with audit checklist generated per-field from the schema. Compares `extracted_non_null_field_count` vs `parser_hypothesis_relevant_count`; raises `gap_signal` above `coverage_gap_tolerance`. Output JSON: `missed_fields`, `spurious_fields`, `parser_hypothesis_misses`, `auditor_notes`.
- [2026-05-17] ADD `config/prompts/system/arbiter.j2` — gated arbiter prompt (Gemini Flash). Picks one of three policies: `ACCEPT_EXTRACTOR`, `RE_EXTRACT`, `INVOKE_VMAW`. Conditional warning when `vmaw_available=false` (Phase 1 flags to SME). Output JSON includes `re_extract_hints` (RE_EXTRACT only) and `vmaw_dispatch_brief` (INVOKE_VMAW only).
- [2026-05-17] ADD `config/prompts/metadata_team.j2` — the big one. `{% include 'system/extractor.j2' %}` at the top, then 12 numbered MetadataTeam-specific rules: (1) fax-header noise ignore, (2) vendor inference order with product-marker table, (3) practice-vs-vendor disambiguation with demo.pdf + doc_3 examples, (4) physician location varies (3 placements), (5) title parsing with all-caps→Title Case exception, (6) strict-MRN labels (only "MRN" / "Medical Record #" / "Med Rec #"), (7) ISO date normalization with 5 source-format examples, (8) practice address line splitting, (9) NPI validation, (10) `Test_Name` vs `Procedure` distinction, (11) `report_title` vs `Test_Name`, (12) `llm_confidence_score` calibration. 290 lines after render.
- [2026-05-17] ADD `config/prompts/preprocess/block_profiler.j2` — Gemini 2.5 Flash @ T=0.0. Classifies every DocAI layout block. Closed `text_role` vocabulary of 21 values including `fax_transport_noise`. Closed `target_umbrella_hint` vocabulary of 5 values. Detailed fax-noise detection signatures (dotted-date timestamps, INBOUND NOTIFICATION banner, CSID block).
- [2026-05-17] ADD `config/prompts/preprocess/medical_ner.j2` — Gemini 2.5 Flash @ T=0.0. Post-processes raw SciSpaCy entities (from in-process `en_ner_bionlp13cg_md` + `en_ner_bc5cdr_md` + `en_core_web_sm`) into a typed ParserHypothesis. Categorizes per umbrella, deduplicates across pages, filters entities in irrelevant blocks (refs / fax-noise / disclaimers), adds `target_field_hint` for report_metadata candidates.

### Block C.1 — Core engine: state + errors + observability + schema-driven foundation (2026-05-17)

- [2026-05-17] ADD `core/__init__.py` — re-exports public API: `PipelineState`, `SchemaLoader` / `SchemaSection` / `FieldMeta`, `PromptRenderer`, `ObservabilityManager` / `trace` / `span`, 11-class error hierarchy.
- [2026-05-17] ADD `core/errors.py` — typed exception hierarchy. Root `PipelineError` carries `doc_id`, `retry_safe`, `context` on every subclass. 11 classes: `PreprocessingError` + `DocAIError` / `BlockProfilerError` / `MedicalNERError`, `SchemaError` + `SchemaLoadError` / `SchemaValidationError`, `PromptRenderError`, `AgentError`, `ToolError`.
- [2026-05-17] ADD `core/state.py` — `PipelineState` TypedDict + 7 sub-shapes (`BlockInfo`, `BlockProfile`, `PageText`, `DocProfile`, `ParserHypothesisCandidate`, `ParserHypothesis`, `VerifierScorecard`). `total=False` so nodes set the keys they own. Carries `pipeline_version` literal, `verdict` literal, OTel trace_id, cost + latency accumulators.
- [2026-05-17] ADD `core/observability.py` — `ObservabilityManager.init_from_env()` boots OpenTelemetry SDK + Cloud Trace exporter. `@trace()` decorator (sync + async via `asyncio.iscoroutinefunction`). `span()` context manager. No-op fallback (`_NoopSpan`) when `CLOUD_TRACE_ENABLED=false` or exporter import fails — unit tests + local dry runs don't need GCP.
- [2026-05-17] ADD `core/schema_loader.py` — the central schema-driven piece. `SchemaLoader.from_path()` loads `genomic_pathology_v2.json` with root-shape sanity check. `get_section(name)` returns `SchemaSection(name, description, fields, pydantic_model, raw_json_schema)`. `_build_pydantic_model()` is recursive — nested objects become generated submodels with their own `extra="forbid"`. JSON Schema → Python type map handles primitives, nullable unions, arrays, nested objects.
- [2026-05-17] ADD `core/prompt_renderer.py` — Jinja env with `StrictUndefined` (missing context = loud error). 5 render methods: `render_team_extractor_prompt`, `render_coverage_auditor_prompt`, `render_arbiter_prompt`, `render_block_profiler_prompt`, `render_medical_ner_prompt`. `ToolDescriptor` dataclass for the `available_tools` Jinja variable. Auto-strips the `config/prompts/` prefix so callers can use either full path or template-relative path.

**C.1 smoke test:** all 4 generated Pydantic models validate `ground_truth/demo.json`. All 5 Phase 1 prompts render with real schema context (15,393-char metadata extractor prompt). Negative test for `additionalProperties: false` enforcement passes. `@trace` no-op verified.

### Block C.2 — Core engine: I/O layer (2026-05-17)

- [2026-05-17] ADD `core/gcs_client.py` — async wrapper around `google-cloud-storage`. `GcsUri` parse dataclass. `GCSClient.list_pdfs / iter_pdfs / read_bytes / write / exists`, all via `asyncio.to_thread`. Tenacity retries (3 attempts, exponential 2-10s) on `read_bytes` and `write`. Lazy client init so unit tests can import without GCP credentials.
- [2026-05-17] ADD `core/persistence.py` — `StorageConfig` dataclass + `load_storage_config('phase_1')` reader for `config/storage.yaml`. `Persistence.write_run / write_extraction / write_artifact`, all tagged with `pipeline_version`. BigQuery `insert_rows_json` with retry on transient errors. `extractions_table_fqn` / `runs_table_fqn` / `artifact_uri(doc_id, kind)` / `doc_prefix(doc_id)` helpers.
- [2026-05-17] ADD `core/checkpointer.py` — factory `build_checkpointer()`. Phase 1 returns LangGraph's built-in `MemorySaver` (sufficient: no human-in-the-loop interrupts yet). Phase 4 stub for `FirestoreCheckpointer` raises `NotImplementedError` with the Phase-4 implementation sketch in a comment. Env-var override `CHECKPOINTER_BACKEND={memory,firestore}`.
- [2026-05-17] ADD `core/tool_registry.py` — `build_tools_for_state(state, allowlist, schema_loader)` closure-bound factory. 7 LangChain `StructuredTool` handlers + Pydantic input schemas: `pdf_page_loader`, `pdf_text_search`, `docai_layout_lookup`, `npi_validator` (CMS Luhn-mod-10 with "80840" prefix), `date_parser` (dateutil + trailing-sex-fragment stripper), `schema_validate` (calls SchemaLoader.validate), `state_read` (whitelist of doc_profile/parser_hypothesis/block_profiles). `descriptors_from_yaml()` bridges to PromptRenderer.
- [2026-05-17] MODIFY `core/__init__.py` — added C.2 re-exports: `GCSClient`, `GcsUri`, `Persistence`, `StorageConfig`, `load_storage_config`, `build_checkpointer`, `build_tools_for_state`, `descriptors_from_yaml`, `ToolDescriptor`. 28 total exports now in `__all__`.
- [2026-05-17] MODIFY (config) `pyproject.toml` — removed `langgraph-checkpoint-firestore` (does not exist on PyPI; would have failed install). Added `langchain>=0.3.0,<0.4` for `StructuredTool` / `@tool` decorator. Comment explaining Phase 1 / Phase 4 checkpointer transition.
- [2026-05-17] MODIFY (config) `requirements.txt` — mirrors the pyproject changes.

**C.2 smoke test:** 28 assertions all pass. All 7 tools execute correctly against a mock PipelineState including the real-world cases from `demo.pdf` (date `01/25/1941 / M` → `1941-01-25`, valid CMS test NPI `1467560003`). Schema-validate tool round-trips `ground_truth/demo.json` `report_metadata` and rejects an injected extra field. `state_read` whitelist enforcement rejects `team_outputs` with `ToolError`. Checkpointer factory returns a real `BaseCheckpointSaver` subclass for the memory backend and raises cleanly for firestore (Phase 4) / unknown backends.

### Environment install + verification (2026-05-17)

- [2026-05-17] MODIFY (config) `requirements.txt` — removed `medspacy` + `hgvs` from the Phase 1 install set. Both are Phase 2-only deps; `hgvs` requires `libpq-dev` for its psycopg2 build which causes Phase 1 installs to fail on systems without Postgres dev headers. Pointer comment added.
- [2026-05-17] ADD `requirements-phase2.txt` — `medspacy>=1.3.0,<2.0` + `hgvs>=1.5.4,<2.0`. Header documents the `libpq-dev` prerequisite (brew install libpq / apt-get install libpq-dev) and the layer-on-top-of-Phase-1 install pattern.
- [2026-05-17] MODIFY (config) `pyproject.toml` — removed medspacy + hgvs from the default `[project.dependencies]`. Added `[project.optional-dependencies].phase2` extras group. `pip install -e ".[phase2]"` is the alternate install path for the Phase 2 deps.
- [2026-05-17] ADD `scripts/__init__.py` — package marker.
- [2026-05-17] ADD `scripts/verify_environment.py` — environment check tool. `--quick` validates Python version + 26 Phase 1 packages + `import core` + SchemaLoader + PromptRenderer + 3 SciSpaCy / spaCy models + storage.yaml. Without `--quick` also pings GCS bucket reachability, DocAI processor lookup, and a Gemini Flash "pong" request. Exit 0 on all-pass, exit 1 on any failure. 37 checks total.
- [2026-05-17] MODIFY `README.md` — install section now documents the Phase 1 / Phase 2 install split, the verify_environment.py step, and the proxy-allowlist note for environments that block the S3 / GitHub model hosts.

**Sandbox install confirmation (2026-05-17):** All 26 Phase 1 Python packages install cleanly in the sandbox (Python 3.10.12 — close enough for spaCy 3.7.x compatibility); 3 of 3 SciSpaCy / spaCy model wheels are blocked by the sandbox proxy allowlist (`X-Proxy-Error: blocked-by-allowlist` on `s3-us-west-2.amazonaws.com` AND `release-assets.githubusercontent.com`). The URLs themselves are correct and will install on any unrestricted network. After installing the 26 packages, the full C.1+C.2 foundation smoke test was re-run against the real packages (not mocks) and all 28 assertions still pass — schema-loader generates correct Pydantic models, prompt renderer produces a 15,206-char metadata_team prompt, storage config loads, checkpointer factory returns InMemorySaver (BaseCheckpointSaver subclass), all 7 tools exercise correctly including ISO date normalization on `08/13/2025 01:15:00 PM PDT` → `2025-08-13`. `scripts/verify_environment.py --quick` returns 3 failures (all 3 spaCy models, as expected in this sandbox) and exit code 1.

### Real-machine install troubleshooting (2026-05-17)

User's macOS dev box (Intel, Python 3.12.8 from python.org, Xcode CLI clang 11.0.3) hit a cascade of install issues — fixes applied to keep `requirements.txt` resolvable on a non-trivial macOS environment:

- [2026-05-17] MODIFY (config) `requirements.txt` / `pyproject.toml` — relaxed `requires-python` from `>=3.11,<3.12` to `>=3.10,<3.13` (spaCy 3.7.x supports the wider range; the user has 3.12.8 from python.org).
- [2026-05-17] ADD `setup.sh` — one-shot Mac installer. Auto-detects `python3.11` / `python3.12` / `python3.10` in that order; installs python@3.11 via Homebrew if none found. Installs `poppler`. Creates `.venv`. `pip install -r requirements.txt`. Copies `.env.example → .env`. Runs `verify_environment.py --quick`. Idempotent / re-runnable.
- [2026-05-17] MODIFY (config) `requirements.txt` / `pyproject.toml` — flipped SciSpaCy from `0.5.4` → `0.5.5`. The 0.5.4 version pinned `scipy<1.11`, no scipy wheels for cp312, source build of scipy 1.9.3 fails on macOS without gfortran. 0.5.5 dropped the scipy cap → resolver picks scipy 1.14.x with native cp312 wheels. Model wheels kept at v0.5.4 (v0.5.5 wheels don't exist on AI2 S3 — HTTP 404 verified); model files declare `scispacy>=0.5.4` which 0.5.5 satisfies.
- [2026-05-17] **MODIFY (config) `requirements.txt` / `pyproject.toml` — REPLACED `langchain-google-vertexai>=2.0.7,<3.0` with `langchain-google-genai==4.2.2`.** Underlying `google-genai` SDK supports both Google AI Studio (dev, `GOOGLE_API_KEY`) and Vertex AI (prod, BAA-covered) via the `GOOGLE_GENAI_USE_VERTEXAI` env flag — one wrapper, two backends. Dropped bottleneck / pandas / scipy from the critical transitive-dep path (the original Vertex wrapper required `bottleneck<2` which broke on the user's older Xcode toolchain).
- [2026-05-17] MODIFY `.env.example` — added `GOOGLE_GENAI_USE_VERTEXAI` (default `true`), `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`. Explicit PHI safety note: dev mode (API key) is NOT BAA-covered and must NOT be used with real patient data.
- [2026-05-17] MODIFY `scripts/verify_environment.py` — `langchain-google-vertexai` → `langchain-google-genai`; `google-genai` added to verified packages. `check_vertex_gemini()` renamed to `check_gemini()` and now uses the unified `ChatGoogleGenerativeAI` invoker which respects `GOOGLE_GENAI_USE_VERTEXAI`. Backend (Vertex vs AI Studio) reported in the OK line.
- [2026-05-18] MODIFY `scripts/verify_environment.py` — added repo-root `sys.path.insert(0, ...)` at the top so `from core...` imports resolve regardless of cwd. Without this, `make verify` failed `import core` because `python scripts/verify_environment.py` doesn't put repo root on path automatically.
- [2026-05-18] DEPRECATED `pyproject.toml` — replaced by standalone tool config files. The file lives on disk as a stub pointing at the replacements (sandbox lacks delete permission); user removes it manually.
- [2026-05-18] ADD `ruff.toml` — linter + formatter config moved out of pyproject.toml. Adds `extend-exclude` for `.venv` / `snapshots` / `data` / `ground_truth`. Adds `[lint.per-file-ignores]` for tests + scripts.
- [2026-05-18] ADD `mypy.ini` — strict type-check config. Excludes `.venv` / `snapshots` / `data` / `ground_truth`. Relaxed `disallow_untyped_defs` for tests + scripts.
- [2026-05-18] ADD `pytest.ini` — pytest config with `asyncio_mode = auto`. Filters spaCy `UserWarning` and google `DeprecationWarning`.
- [2026-05-18] ADD `Makefile` — 16 dev workflow targets: `setup`, `install` / `install-phase2`, `venv`, `verify` / `verify-cloud`, `lint` / `lint-fix` / `format`, `typecheck`, `test` / `test-cov`, `run-local PDF=...`, `ui PHASE=N`, `snapshot PHASE=N`, `clean-cache` / `clean-venv`. Uses explicit `.venv/bin/python` paths so commands work without sourcing the venv. `make help` is the default target and lists everything.
- [2026-05-18] MODIFY `README.md` — install section rewritten around `make setup` / `make verify`; table of make targets added; explicit note that there is no pyproject.toml for this project.

**Real-machine install confirmation (2026-05-18):** All 27 Phase 1 Python packages installed via `make setup` on macOS Intel + Python 3.12.8. Resolver picked `scispacy 0.6.2` (newer than the 0.5.5 we'd been chasing — works fine with the v0.5.4 model wheels). All 3 NER models load and identify entities. `make verify` reports **All 38 checks passed. Environment is ready.** Foundation locked, ready for Block D.

### Block D.1 — Preprocessing: DocAI parser + fax-header filter + Block Profiler (2026-05-18)

- [2026-05-18] ADD `preprocess/__init__.py` — package surface: re-exports `DocAIParser`, `FaxHeaderFilter`, `BlockProfiler`. Doc header diagrams the output flow per document.
- [2026-05-18] ADD `preprocess/docai_parser.py` — `DocAIParser` class wrapping DocAI Layout Parser. `parse(doc_id, gcs_uri) → (DocProfile, raw_response_uri)`. Sync DocAI client wrapped in `asyncio.to_thread`. Tenacity retries (3 attempts, exponential 2-10s) on transient errors. Caches raw DocAI Document JSON to GCS via the Persistence class. Walks `document_layout.blocks` recursively to flatten into `BlockInfo[]` with bbox + section_path; falls back to per-page `paragraphs` for older processors. Lazy client init.
- [2026-05-18] ADD `preprocess/fax_header_filter.py` — `FaxHeaderFilter` class with 5 deterministic regex patterns: (1) `** INBOUND NOTIFICATION : FAX RECEIVED SUCCESSFULLY **` banner, (2) multi-column TIME RECEIVED / REMOTE CSID / DURATION / PAGES / STATUS header, (3) CSID timestamp `MM.DD.YYYY HH:MM:SS Page: N of M Sender: ... Recipient: +<digits>` (dotted-date discriminator), (4) standalone `Page: N of M Sender: ... Recipient:` fragment, (5) data row `<date> at HH:MM:SS ... Received|Success` (catches `August 26, 2025 at 4:42:57 PM PDT NeoGenomics 91 2 Received`). Line-by-line stripper preserves slash-separator dates so legitimate `Collection_Date` / `Received_Date` / `Report_Date` lines survive.
- [2026-05-18] ADD `preprocess/block_profiler.py` — `BlockProfiler` class. One Gemini 2.5 Flash @ T=0.0 call per doc via `langchain-google-genai 4.2.2`'s `ChatGoogleGenerativeAI.with_structured_output(BlockProfilerOutput)`. Pre-tags fax-noise blocks deterministically (skips Gemini for those — saves tokens + guarantees correctness). 21-value `TextRole` Literal + 5-value `TargetUmbrellaHint` Literal enforced via Pydantic. Sanity checks 1:1 input→output block count and block_id set parity; raises `BlockProfilerError` on divergence.

**D.1 smoke test:** all 3 modules import cleanly; `preprocess.__all__` exports 3 names. `FaxHeaderFilter.filter()` against the actual `demo.pdf` fax band correctly strips all 4 noise patterns (banner, column-header, CSID timestamps p1+p2, status row) AND preserves all legitimate report content — including the 3 trickiest cases that contain "Received" or look date-like (`Collection Date: 08/12/2025`, `Received Date: 08/13/2025 01:15:00 PM PDT`, `Report Date: 08/26/2025 07:41:01 PM ET`). No false-positives.

### Block D.2 — Preprocessing: in-process Medical NER + preprocess_node orchestrator (2026-05-18)

- [2026-05-18] ADD `preprocess/medical_ner.py` — `SciSpaCyMedicalNER` class. Lazy-loads `en_ner_bionlp13cg_md` + `en_ner_bc5cdr_md` + `en_core_web_sm` once via `asyncio.to_thread`; cached for the process lifetime via `asyncio.Lock`-protected init. `extract_raw_entities()` runs all 3 spaCy pipelines per page and returns a flat list of `{text, label, page, char_start, char_end, source_model}`. `post_process()` invokes Gemini 2.5 Flash @ T=0.0 via `ChatGoogleGenerativeAI.with_structured_output(ParserHypothesisOutput)`; injects per-page `block_profiles_on_page` so Gemini can drop entities in fax-noise / reference blocks. Empty-input shortcut returns empty hypothesis without making a Gemini call. Output validated against the 5-value closed-vocab `TargetUmbrella` Literal (`drop` entries are filtered before returning).
- [2026-05-18] ADD `preprocess/preprocess_node.py` — `PreprocessNodeDependencies` dataclass (5 components) + `make_preprocess_node(deps)` factory returning an async LangGraph node. Sequencing: DocAI parse → fax_filter (sync, deterministic) → **`asyncio.gather(BlockProfiler.profile, MedicalNER.extract_raw_entities)`** → NER post-processor (sequential, now sees block_profiles) → `asyncio.gather` of two `persistence.write_artifact` calls (block_profiles + parser_hypothesis to GCS). All-or-nothing: any failure bubbles up as `PreprocessingError`; routes the doc to SME without partial state. Returns LangGraph state delta with `doc_profile`, `parser_hypothesis`, `artifacts_gcs_prefix`, and additive `latency_ms`. `run_preprocess(state, deps)` helper for direct invocation in tests.
- [2026-05-18] MODIFY `preprocess/__init__.py` — added `SciSpaCyMedicalNER`, `PreprocessNodeDependencies`, `make_preprocess_node`, `run_preprocess` to `__all__`. Package now exports 7 names; downstream pipeline assembly imports them in one shot.

**D.2 smoke test (mocked DocAI + mocked Gemini — no cloud credentials needed):** all imports resolve; `make_preprocess_node` runs end-to-end and returns a correct state delta with `doc_profile.total_pages=2`, 5 blocks classified (2 pre-tagged `fax_transport_noise`, 3 classified by Gemini mock), 2 ParserHypothesis candidates with correct `counts_by_umbrella` ({report_metadata: 1, tested_biomarker_umbrella: 1}), `artifacts_gcs_prefix=gs://patient_clinical_trial/extraction_outputs/phase1/demo/`, and both expected GCS writes (`block_profiles.json` + `parser_hypothesis.json`). Fax filter strips `INBOUND NOTIFICATION` from page 1 and `08.27.2025 ... Page: 2 of 2` from page 2 while preserving `Molecular Genetics` and `Clinical Significance`.

**Phase 1 preprocessing layer (D.1 + D.2) is now complete.** Next blocks (E + F + G + H) build agents, the MetadataTeam subgraph, verifier + decision router, and the graph_v1 composition that wires preprocess → MetadataTeam → schema_validator → decision → persist.

### Block E — Agents: base + Extractor / Coverage Auditor / Arbiter (2026-05-18)

- [2026-05-18] ADD `agents/__init__.py` — public API: 12 exports (Agent, AgentResult, Extractor, ExtractorResult, CoverageAuditor, AuditorResult, MissedField, SpuriousField, ParserHypothesisMiss, Arbiter, ArbiterResult, ArbiterPolicy).
- [2026-05-18] ADD `agents/base.py` — abstract `Agent` base. Holds `model_name` + `temperature` (T≠0.0 logs a warning per project policy). `_make_llm()` builds the `ChatGoogleGenerativeAI` wrapper that auto-selects Vertex AI vs Google AI Studio via `GOOGLE_GENAI_USE_VERTEXAI`. `_invoke_structured(prompt, response_model)` is the shared single-shot path used by Auditor + Arbiter (uses `method="function_calling"`, more reliable than json_mode for nested schemas). `AgentResult` dataclass carries agent_id, model_name, reasoning_trace[], tool_calls_made, llm_confidence_score, latency_ms, tokens_input/output, warnings[]. `_extract_usage()` best-effort token-usage extraction from langchain AIMessage.
- [2026-05-18] ADD `agents/extractor.py` — `Extractor(Agent)`. Manual ReAct loop, capped at `MAX_ITERATIONS=12`. Tools bound via `build_tools_for_state` to the current PipelineState per invocation (no race conditions on concurrent docs). Tool calls within one iteration run via `asyncio.gather`. Final-answer JSON validated against the team's Pydantic model from `schema_loader.validate()`; one retry on malformed JSON with an injected correction message before raising AgentError. Defensive regex (`_FINAL_JSON_RE`) to peel JSON out of stray markdown fences. `_build_user_message` injects per-page text + optional Arbiter `re_extract_hints` from a previous attempt. 5-entry reasoning trace captured per turn (role, content, tool_calls).
- [2026-05-18] ADD `agents/coverage_auditor.py` — `CoverageAuditor(Agent)`. Single LLM call, no tools. Pydantic-enforced output: `coverage_ok`, `gap_signal`, `missed_fields[]` (each: field_name + evidence_location + extracted_value_from_source + why_extractor_should_have_caught_it), `spurious_fields[]`, `parser_hypothesis_misses[]`, `auditor_notes`. `coverage_gap_tolerance` injected from `teams.yaml`. Caller responsible for filtering parser_hypothesis candidates to this team's umbrella before passing in.
- [2026-05-18] ADD `agents/arbiter.py` — `Arbiter(Agent)`. Single LLM call, **defaults to Gemini Flash** (cheaper — Pro overkill for 3-way classification). Literal-enforced `ArbiterPolicy = "ACCEPT_EXTRACTOR" | "RE_EXTRACT" | "INVOKE_VMAW"`. Conditional `vmaw_dispatch_brief` only for `INVOKE_VMAW`. Logs warning when Phase 1's `vmaw_available=False` blocks the chosen policy (caller routes to SME).

**Block E smoke test:** 4 scenarios pass against mocked LLM.
1. Extractor 3-iteration ReAct (search + parse_date + final answer) produces validated `report_metadata` JSON with `Patient_First_Name="William"`, `Vendor_Name="NeoGenomics"`, `Patient_MRN=null`, `llm_confidence_score=0.95`.
2. CoverageAuditor returns `coverage_ok=False, gap_signal=True, missed_fields=[Practice_Street_Address_1]`.
3. Arbiter picks `RE_EXTRACT` with concrete hints.
4. Arbiter picks `INVOKE_VMAW` with populated dispatch brief; logs warning because `vmaw_available=False` in Phase 1.

### Block F — MetadataTeam subgraph (2026-05-18)

- [2026-05-18] ADD `teams/__init__.py` — public API: `MetadataTeam`, `MetadataTeamResult`, `TeamVerdict` Literal.
- [2026-05-18] ADD `teams/metadata_team.py` — `MetadataTeam` class. Composes Extractor → CoverageAuditor → conditional Arbiter via a plain async orchestrator (LangGraph subgraph wasn't needed at this scale — `run()` is ~120 lines and the conditional flow is clearer as straight Python). 4 code paths: happy commit / Arbiter ACCEPT_EXTRACTOR / Arbiter RE_EXTRACT (one retry round, `MAX_RE_EXTRACT_ROUNDS=1`) / Arbiter INVOKE_VMAW (Phase 1 → SME flag because `vmaw_available=False`). Aggregates token usage + tool calls + latency across agents into `MetadataTeamResult`. Defensive: if Arbiter picks RE_EXTRACT with no hints, route to SME flag (Arbiter prompt contract violated). Filters `parser_hypothesis.candidates` to `target_umbrella=="report_metadata"` before passing to the Auditor (keeps the class team-agnostic for Phase 2 reuse).

**Block F smoke test:** all 4 paths pass against mocked agents.
1. Happy commit — Auditor approves, Arbiter NOT called, output committed.
2. Arbiter ACCEPT_EXTRACTOR — Auditor flagged but Arbiter overruled, no retry, output committed.
3. Arbiter RE_EXTRACT — Extractor ran 2x (original + retry-with-hints), retry output (`Practice_City="Murrieta"`) committed, retry trace preserved.
4. Arbiter INVOKE_VMAW — `verdict=sme_flag`, `output=None`, full VMAW dispatch brief preserved in `verdict_reason` for downstream SME UI.

Token aggregation across agents confirmed (`total_tokens_input = 2000 + 1500 = 3500` on happy path: extractor + auditor only).

### Block G — Verification + Decision (2026-05-18)

- [2026-05-18] ADD `verification/__init__.py` — exports `SchemaValidator`, `build_phase1_envelope`, `make_schema_validator_node`.
- [2026-05-18] ADD `verification/schema_validator.py` — `SchemaValidator.validate_envelope(envelope)` runs per-section Pydantic validation via `SchemaLoader.validate()` plus root-level `jsonschema.Draft7Validator` on the assembled envelope. `build_phase1_envelope(report_metadata)` wraps the MetadataTeam output into the schema-v2 envelope with empty placeholders for the other 3 umbrellas (Phase 2 replaces this with the Linking agent's proper multi-team merge). `make_schema_validator_node(schema_loader=...)` returns an async LangGraph node that emits a `VerifierScorecard` and (on pass) sets `state.extraction` to the validated envelope. Team-sme-flag short-circuit: when `state._team_verdict == "sme_flag"`, the validator emits a passed=True scorecard with a note and skips envelope assembly (decision router handles the flag).
- [2026-05-18] ADD `decision/__init__.py` — exports `DecisionRouter`, `make_decision_router_node`.
- [2026-05-18] ADD `decision/decision_router.py` — `DecisionRouter.decide(state) → (verdict, reason)`. Pure function of state, 4-rule resolution policy (top match wins): (1) team `sme_flag` propagates, (2) any verifier scorecard with `passed=False` → sme_flag, (3) `llm_confidence_score < threshold` → sme_flag, (4) else auto_accept. Threshold from `AUTO_ACCEPT_CONFIDENCE_THRESHOLD` env (default 0.85, matches `teams.yaml`).

### Block H — Pipeline graph + runner (2026-05-18)

- [2026-05-18] ADD `pipeline/__init__.py` — exports `build_graph_v1`, `build_graph_v1_dependencies`, `run`, `RunResult`.
- [2026-05-18] ADD `pipeline/graph_v1.py` — frozen Phase 1 LangGraph composition. `GraphV1Dependencies` dataclass bundles 7 components. `build_graph_v1_dependencies()` is the one-shot factory that reads `config/storage.yaml` + `config/teams.yaml` and constructs every dep (DocAI / Block Profiler / SciSpaCyMedicalNER / Extractor / CoverageAuditor / Arbiter / MetadataTeam / Persistence). `build_graph_v1(deps)` compiles the linear graph with 6 nodes (`document_received` → `preprocess` → `metadata_team` → `schema_validator` → `decision_router` → `persist` → `END`) using `StateGraph(PipelineState)` and the checkpointer factory. `_document_received_node` sanity-checks seed state. `_make_metadata_team_node` wraps `MetadataTeam.run()` and surfaces the team's `verdict` / `verdict_reason` into hidden `_team_verdict` / `_team_verdict_reason` state keys (consumed by schema_validator + decision_router). `_make_persist_node` always writes the `runs_v1` row; writes the `extractions_v1` row only when `verdict=auto_accept` AND `extraction` is present. Non-blocking on persistence errors (logs + continues — Phase 1 prioritizes finishing the run over BigQuery availability).
- [2026-05-18] ADD `pipeline/runner.py` — `RunResult` dataclass + `await run(doc_id, gcs_uri, version="v1") → RunResult`. Boots `ObservabilityManager` at entry. Phase 1 errors on `version != "v1"`. Generates unique `thread_id` (doc_id + uuid hex prefix) per invocation for the checkpointer. Captures any pipeline error into `RunResult.error` (verdict="errored") rather than re-raising — the caller (CLI, Dataflow) gets a structured result either way.

**Block G + H smoke tests:** all 4 paths pass against mocked agents + persistence.
1. SchemaValidator validates the demo envelope (`passed=True, errors=0`) and rejects extra fields with correct error location.
2. DecisionRouter: 4 cases — team sme_flag propagates, verifier failure → sme_flag, low confidence → sme_flag, all-clear → auto_accept.
3. **Full graph_v1 AUTO_ACCEPT path** — runner.run() returns `verdict=auto_accept`, extraction has all 4 umbrellas with `Vendor_Name="NeoGenomics"` from the team output, `count_of_extracted_objects=0` (Phase 1 default), both `write_run` and `write_extraction` called.
4. **Full graph_v1 SME_FLAG path** (low confidence) — `verdict=sme_flag`, `write_run` called (run row IS written for SME triage), `write_extraction` NOT called (extraction skipped per the rule).

**Real bug caught + fixed during smoke testing:** LangGraph 1.x's `StateGraph(dict)` treats opaque dict as full-replacement state — the seed `doc_id`/`gcs_uri`/`pipeline_version` were getting dropped before reaching the first node. Switched to `StateGraph(PipelineState)` (the typed-dict from core/state.py) so LangGraph preserves un-returned keys across nodes. One-line code change with an explanatory inline comment.

### Block I — Local scripts (process_local + snapshot_phase)

_pending — `scripts/verify_environment.py` already added under the Environment install step above_

### Block J — Initial end-to-end validation

_pending_

### Block K — Streamlit UI for Phase 1

_pending_

### Block L — Demo prep, snapshot, manifest, git tag

_pending_

---

## File index (will be populated continuously)

This section gets the canonical list of every file added in Phase 1, grouped by directory. Used directly for the transfer commit.

### config/
- `config/schemas/genomic_pathology_v2.json`
- `config/storage.yaml`
- `config/teams.yaml`
- `config/tools.yaml`
- `config/prompts/system/extractor.j2`
- `config/prompts/system/coverage_auditor.j2`
- `config/prompts/system/arbiter.j2`
- `config/prompts/metadata_team.j2`
- `config/prompts/preprocess/block_profiler.j2`
- `config/prompts/preprocess/medical_ner.j2`

### ground_truth/
- `ground_truth/demo.json`
- `ground_truth/phase1/doc_3.json`
- `ground_truth/phase1/doc_11.json`
- `ground_truth/phase1/doc2_1.json`
- `ground_truth/phase1/doc2_6.json`
- `ground_truth/phase1/doc2_25.json`
- `ground_truth/phase1/README.md`

### project-level
- `requirements.txt`
- `requirements-phase2.txt`
- `.env.example`
- `.gitignore`
- `README.md`
- `setup.sh`
- `Makefile`
- `ruff.toml`
- `mypy.ini`
- `pytest.ini`
- `PHASE_1_PLAN.md`
- `PHASES.md`
- `MANIFEST_PHASE_1.md`

_`pyproject.toml` was removed — it duplicated `requirements.txt` for no benefit (we never publish to PyPI or run `pip install -e .`). Tool configs moved to standalone files (`ruff.toml`, `mypy.ini`, `pytest.ini`); dev workflows moved to `Makefile`._

### core/
- `core/__init__.py`
- `core/errors.py`
- `core/state.py`
- `core/observability.py`
- `core/schema_loader.py`
- `core/prompt_renderer.py`
- `core/gcs_client.py`
- `core/persistence.py`
- `core/checkpointer.py`
- `core/tool_registry.py`
### preprocess/
- `preprocess/__init__.py`
- `preprocess/docai_parser.py`
- `preprocess/fax_header_filter.py`
- `preprocess/block_profiler.py`
- `preprocess/medical_ner.py`
- `preprocess/preprocess_node.py`
### agents/
- `agents/__init__.py`
- `agents/base.py`
- `agents/extractor.py`
- `agents/coverage_auditor.py`
- `agents/arbiter.py`
### teams/
- `teams/__init__.py`
- `teams/metadata_team.py`
### verification/
- `verification/__init__.py`
- `verification/schema_validator.py`

### decision/
- `decision/__init__.py`
- `decision/decision_router.py`

### pipeline/
- `pipeline/__init__.py`
- `pipeline/graph_v1.py`
- `pipeline/runner.py`
### scripts/ — _pending_
### ui/phase1/ — _pending_
### snapshots/phase1/ — _pending_
