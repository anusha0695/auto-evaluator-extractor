# extractor — Agentic Ground-Truth Extraction for Genomic / Pathology Reports

A LangGraph-orchestrated multi-agent pipeline that turns genomic and pathology PDF reports into structured ground-truth JSON conforming to `config/schemas/genomic_pathology_v2.json`. Runs on GCP (`medical-report-extraction`) with PHI-safe in-project observability.

This is the **dev workspace**. Production transfer happens at the end of all 4 phases — see [`PHASES.md`](PHASES.md).

---

## Phase status

| Phase | Status | What it ships |
|---|---|---|
| 1 | **in progress** | Foundation + MetadataTeam (one schema section) end-to-end |
| 2 | planned | 4 Specialist Teams + Planner + Linking + full verifier suite |
| 3 | planned | VMAW fact-checker + chunk-level SME review UI |
| 4 | planned | Full SME approve/edit/reject loop + Vertex Vector Search learning loop + Dataflow deploy |

Detailed plans live in [`PHASES.md`](PHASES.md) (all 4 phases) and [`PHASE_1_PLAN.md`](PHASE_1_PLAN.md) (work blocks for the current phase).

---

## What's in this repo

```
extractor/
├── config/
│   ├── schemas/genomic_pathology_v2.json    # source-of-truth JSON Schema (4 umbrella sections)
│   ├── storage.yaml                          # per-phase BigQuery + GCS destinations
│   ├── teams.yaml                            # team → schema section mapping
│   ├── tools.yaml                            # shared tool registry
│   └── prompts/                              # Jinja templates that pull from the schema
├── core/                                     # schema-independent engine (frozen after Phase 1)
├── preprocess/                               # DocAI + fax-header filter + Block Profiler + in-process SciSpaCy NER
├── agents/                                   # Extractor / Coverage Auditor / Arbiter (Phase 1); Planner / Linking / VMAW (later)
├── teams/                                    # MetadataTeam (Phase 1); 3 more in Phase 2
├── verification/                             # Schema validator (Phase 1); 3 more in Phase 2
├── decision/                                 # Auto-Accept vs SME-Flag router
├── pipeline/                                 # graph_v1.py (Phase 1, frozen) → graph_v2.py → ...
├── scripts/                                  # process_local.py, snapshot_phase.py, deploy_dataflow.py (Phase 4)
├── ui/                                       # ui/phase1/, ui/phase2/, ... — Streamlit apps, one per phase
├── snapshots/                                # frozen per-phase UI + graph for replay demos
├── ground_truth/
│   ├── demo.json                             # PRIMARY Phase 1 validation (NeoGenomics demo.pdf, all 4 umbrellas)
│   └── phase1/                               # 5 synthetic mock fixtures (report_metadata only)
├── MANIFEST_PHASE_1.md                       # live shipping manifest for Phase 1
├── PHASE_1_PLAN.md                           # Phase 1 work blocks + 10-day schedule
├── PHASES.md                                 # master plan for all 4 phases
└── README.md                                 # you are here
```

---

## Prerequisites

### Local

- **Python 3.11** (`scispacy` pins spaCy to `>=3.7.5,<3.8`, which has Python 3.11 wheels).
- **poppler-utils** for the Streamlit PDF viewer:
  - macOS: `brew install poppler`
  - Debian/Ubuntu: `apt-get install poppler-utils`
- **gcloud CLI** authenticated to project `medical-report-extraction`.
- **Application Default Credentials** configured: `gcloud auth application-default login`.

### Cloud (provisioning tickets — see `PHASE_1_PLAN.md` § Prerequisites)

- GCP project `medical-report-extraction` with billing enabled.
- DocAI Layout Parser processor `pretrained-layout-parser-v1.6-2026-01-13` in location `us`, processor ID `81e83f6783d90bb0`.
- GCS bucket `gs://patient_clinical_trial/` with `patient_profiles/` (inputs) and `extraction_outputs/phase1/` (outputs).
- BigQuery dataset `extractor_dev` with tables `extractions_v1` + `runs_v1`.
- Firestore database (Native mode) for the LangGraph checkpointer.
- Service account `pipeline-preprocess@medical-report-extraction.iam.gserviceaccount.com` with appropriate roles.
- BAA confirmation from compliance for DocAI + Vertex AI Gemini.

---

## Install

The whole flow is driven by `make`. Run `make help` to see every target.

```bash
# 1. One-shot setup — installs Homebrew prereqs, creates .venv, pip installs,
#    runs the local verify. Idempotent: re-running is safe.
make setup

# 2. Edit .env with your real GCP project / DocAI / bucket values
cp .env.example .env       # if not already done by `make setup`
open -e .env

# 3. Verify the environment
make verify                # local checks only (no cloud calls)
make verify-cloud          # also pings DocAI, GCS, Vertex Gemini

# 4. (Optional, when Phase 2 work begins) Phase 2 deps
#    Requires libpq-dev on the host (brew install libpq / apt-get install libpq-dev).
make install-phase2
```

The `make verify` target exits 0 on success, 1 on failure, and prints per-check
OK/FAIL lines.

### Other useful targets

| Command | What it does |
|---|---|
| `make install` | `pip install -r requirements.txt` into `.venv` (creates venv if missing) |
| `make lint` | Ruff lint check |
| `make lint-fix` | Ruff lint with safe auto-fixes |
| `make format` | Ruff format (in place) |
| `make typecheck` | Mypy strict on `core/` + `preprocess/` + agents / teams / verification / decision / pipeline / scripts |
| `make test` | pytest |
| `make test-cov` | pytest with coverage report |
| `make run-local PDF=gs://...` | Run the pipeline on one PDF |
| `make ui PHASE=1` | Launch the Streamlit results browser |
| `make snapshot PHASE=1` | Snapshot a phase to `snapshots/phaseN/` |
| `make clean-cache` | Remove `__pycache__` / `.pytest_cache` / `.mypy_cache` / `.ruff_cache` |
| `make clean-venv` | Wipe `.venv` entirely (forces full reinstall) |

Tool configs live in standalone files: `ruff.toml`, `mypy.ini`, `pytest.ini`.
There is no `pyproject.toml` — for an internal application, it just duplicated
the deps from `requirements.txt`.

**Model download note:** the SciSpaCy `en_ner_*` model wheels live at
`s3-us-west-2.amazonaws.com/ai2-s2-scispacy/` (each ~150-300 MB). On corporate
networks with proxy allowlists, you may need to allow that S3 host and
GitHub release-assets CDN explicitly. If `pip install -r requirements.txt`
fails on the lines starting with `https://`, the network is the issue, not the
URLs (which are pinned to scispacy 0.5.5 + spaCy 3.7.1).

---

## Phase 1 — running the pipeline

The pipeline runs **out-of-band** (UI is read-only). Upload PDFs to GCS, then invoke `process_local.py`.

```bash
# Upload demo.pdf to the input bucket
gsutil cp data/actual_docs/demo.pdf gs://patient_clinical_trial/patient_profiles/

# Process a single doc (Phase 1, graph_v1)
python scripts/process_local.py \
  --gcs-uri gs://patient_clinical_trial/patient_profiles/demo.pdf \
  --version v1

# Or process every PDF in the input prefix
python scripts/process_local.py \
  --list-bucket gs://patient_clinical_trial/patient_profiles/ \
  --version v1
```

Outputs:

- Raw DocAI response → `gs://patient_clinical_trial/extraction_outputs/phase1/<doc_id>/docai-raw.json`
- Block Profiler output → `gs://patient_clinical_trial/extraction_outputs/phase1/<doc_id>/block_profiles.json`
- SciSpaCy parser hypothesis → `gs://patient_clinical_trial/extraction_outputs/phase1/<doc_id>/parser_hypothesis.json`
- Final `report_metadata` extraction → BigQuery table `medical-report-extraction.extractor_dev.extractions_v1`
- Run metadata (verdict, cost, latency) → BigQuery table `medical-report-extraction.extractor_dev.runs_v1`
- LangGraph checkpoint → Firestore collection `langgraph_checkpoints_phase1`
- OpenTelemetry trace → Cloud Trace, tagged with `doc_id` and `pipeline_version`

---

## Phase 1 — browsing results in the Streamlit UI

```bash
streamlit run ui/phase1/app.py
# → opens at http://localhost:8501
```

The UI is **read-only** — it queries BigQuery `runs_v1` for already-processed docs and renders the persisted artifacts. It does not trigger pipeline runs.

For demos, lead with `demo.pdf` — it's the real NeoGenomics fax, which exercises the fax-header filter, vendor-recognition rule, and practice-vs-vendor disambiguation.

---

## Acceptance gate for Phase 1

Pipeline correctness **≥ 80% per-field match** of `genomic_pathology_extraction.report_metadata` vs. `ground_truth/demo.json` (primary) and the 5 fixtures at `ground_truth/phase1/*.json` (secondary).

Full acceptance criteria: see [`PHASE_1_PLAN.md` § Acceptance criteria](PHASE_1_PLAN.md).

---

## PHI handling

PHI **never leaves the GCP project** (`medical-report-extraction`):

- DocAI processor lives in the project.
- Vertex AI Gemini calls hit the project's Vertex endpoint — BAA-covered.
- SciSpaCy / MedSpaCy run **in-process** on the pipeline worker — no separate inference endpoint.
- Observability uses OpenTelemetry → Cloud Trace + Cloud Logging + Cloud Monitoring (no LangSmith).
- LangGraph checkpoints persisted to Firestore in the same project.

`local_runs/`, `local_artifacts/`, `scratch/`, and any `.env`-style file are in `.gitignore` to prevent accidental commits of PHI-bearing artifacts.

---

## See also

- [`PHASES.md`](PHASES.md) — master plan, architecture recap, all 4 phases
- [`PHASE_1_PLAN.md`](PHASE_1_PLAN.md) — Phase 1 work blocks + 10-day schedule
- [`config/schemas/genomic_pathology_v2.json`](config/schemas/genomic_pathology_v2.json) — schema source of truth
- [`ground_truth/phase1/README.md`](ground_truth/phase1/README.md) — Phase 1 validation set documentation
- [`MANIFEST_PHASE_1.md`](MANIFEST_PHASE_1.md) — live shipping manifest
