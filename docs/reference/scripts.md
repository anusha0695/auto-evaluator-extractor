# Scripts — Operational Tooling

`scripts/` holds the **operational tooling** (run, score, convert, snapshot, env-check
and data-prep). The **regression gates** live separately in `scripts/gates/` — see
[traceability.md](traceability.md). Each script is standalone; run with
`PYTHONPATH=. python scripts/<name>` or via the `make` target noted below.

| Script | Make target | What it does |
|---|---|---|
| `process_local.py` | `make run-local PDF=… PHASE=…` | The local runner. Drives a PDF through the selected graph (v2 / v3 / v4 — set `PHASE=4` for the v4 self-correcting run) end-to-end and writes artifacts to `local_runs/artifacts/<doc_id>/`. `--json` emits the extraction envelope (e.g. the `demo_v2.json` dump) for scoring. |
| `score_against_ground_truth.py` | — | Scores an extraction envelope against a `ground_truth/*.json` fixture (per-section P/R/F1; supports `--all-sections` with an `--extraction-file`). Excludes `provenance` from scoring. |
| `to_production.py` | `make to-production DOC=…` | Converts a saved extraction to the production schema via `transform/to_production.py`; writes `extraction_production.json`. (graph_selfcorrecting also auto-emits this on committed runs.) |
| `snapshot_phase.py` | — | Snapshots a phase's artifacts/state into `snapshots/` (committed) for a frozen reference point. |
| `verify_environment.py` | `make verify` / `make verify-cloud` | Local environment check (deps, config) with `--quick`; full check pings DocAI / GCS / Gemini. **This is the only `verify_*` script that is tooling, not a milestone gate** — it stays in `scripts/`. |
| `fetch_hgnc.py` | — | Data-prep: fetches the HGNC alias table to (re)build `config/data/hgnc_aliases.tsv`. Run offline-occasionally, not in the pipeline. |
| `make_specimen_pdf.py` | — | Data-prep: builds a synthetic specimen-pathology PDF for dev/testing the specimen team. |
| `push_to_github.sh` | — | Convenience git push helper. |

## How the gates are invoked

```bash
make verify-phase2     # runs every scripts/gates/gate_p2_*.py (sorted)
make verify-phase3     # runs every scripts/gates/gate_p3_*.py (sorted)
PYTHONPATH=. python scripts/gates/gate_p3_m6_triage_repair_loop.py   # one gate
```

The Makefile targets glob `scripts/gates/`; adding a new `gate_p3_m*_*.py` file is picked
up automatically (no Makefile edit needed).

## Where outputs go

- Run artifacts: `local_runs/artifacts/<doc_id>/` — **gitignored** (may contain PHI on
  real documents). Includes `extraction_v2.json`, `verification_v2.json`, `agent_trace.json`,
  `repair_log.json`, `vmaw_log.json`, `escalation_queue.json`, `link_metrics.json`,
  `extraction_production.json`, plus the block/geometry artifacts.
- Extractor debug dumps: `local_runs/_extractor_debug/` — **gitignored**, local-only.
- Snapshots: `snapshots/` — committed.
