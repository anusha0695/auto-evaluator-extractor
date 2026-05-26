# extractor — Agentic Ground-Truth Extraction for Genomic / Pathology Reports

A LangGraph-orchestrated multi-agent pipeline that turns genomic and pathology PDF
reports into structured, nested ground-truth JSON — extracting entities, linking them
into the schema, verifying them with deterministic + evidence-grounded checks,
self-correcting within a bounded budget, and escalating to a human only where the agents
genuinely cannot decide. Runs on GCP with PHI-safe, in-project observability.

## 📖 Documentation

**The full project documentation lives in [`docs/`](docs/README.md).** Start there:

- [`docs/README.md`](docs/README.md) — what it is, the pipeline, repo layout, how to run.
- [`docs/architecture/overview.md`](docs/architecture/overview.md) — flow, graphs, state, agent communication, the ping-back / repair loop, VMAW.
- [`docs/architecture/schema.md`](docs/architecture/schema.md) — the v3 schema, biomarker model, provenance, production mapping.
- [`docs/architecture/components.md`](docs/architecture/components.md) — every component and every check.
- [`docs/reference/traceability.md`](docs/reference/traceability.md) — milestone ↔ gate ↔ source ↔ prompt/config.
- [`docs/reference/scripts.md`](docs/reference/scripts.md) — operational scripts.

## Quickstart

```bash
make setup                 # venv + deps + Homebrew prereqs + local verify (idempotent)
cp .env.example .env       # then fill in your GCP project / DocAI / bucket values
make verify                # local environment check (no cloud calls)
make verify-cloud          # also pings DocAI, GCS, Vertex Gemini

make run-local PDF=/path/to/report.pdf PHASE=3   # full self-correcting run (graph_v3)
make ui                                           # launch the Streamlit review app
make verify-phase2 && make verify-phase3          # the regression gate suite
```

`make help` lists every target. Tool configs are standalone (`ruff.toml`, `mypy.ini`,
`pytest.ini`); there is intentionally no `pyproject.toml`.

## Prerequisites

- **Python 3.11**, **poppler-utils** for the PDF viewer (`brew install poppler` /
  `apt-get install poppler-utils`).
- **gcloud CLI** authenticated to the GCP project, with Application Default Credentials
  (`gcloud auth application-default login`).
- DocAI Layout Parser processor, GCS bucket, and BAA confirmation for DocAI + Vertex
  Gemini. Set `GOOGLE_GENAI_USE_VERTEXAI=true` so model calls use the BAA-covered path.

## PHI handling

PHI **never leaves the GCP project**: DocAI and Vertex Gemini run in-project (BAA),
NER/normalizers run in-process, observability is OpenTelemetry → Cloud Trace/Logging.
`local_runs/`, `local_artifacts/`, `_extractor_debug/`, `prod_prompt_data/`, and any
`.env`-style file are gitignored to prevent accidental commits of PHI-bearing artifacts.
Temperature is locked at 0.0 everywhere for determinism.

## Repository layout

See [`docs/README.md` § 3](docs/README.md) for the full table. In short: `core/`
(engine), `preprocess/` (PDF → blocks), `agents/` (extractor / auditor / arbiter /
planner / linker / verifiers / adjudicators), `teams/`, `pipeline/` (graphs + triage +
repair + vmaw), `verification/`, `decision/`, `transform/`, `ui/`, `config/` (schemas,
prompts, registries), `scripts/` (tooling) + `scripts/gates/` (the regression suite),
`ground_truth/` (fixtures), `docs/`.
