# Extractor — Agentic Ground-Truth Extraction for Genomic / Pathology Reports

This is the project documentation. It explains **what the system is, how it is built,
every component, every check, the agent-to-agent flow, the self-correction loop, and
where everything lives** (prompts, config, gates, scripts). If you are new, read this
page top to bottom, then follow the links into `architecture/` and `reference/`.

> The older `MANIFEST_PHASE_*.md` and `PHASE_*` files were the chronological build
> log used while the system was being constructed. They are kept locally (gitignored)
> as history. **This `docs/` tree is the maintained source of truth.**

---

## 1. What this system does

It turns a genomic / pathology **PDF** into a **structured, nested JSON "ground truth"**
record — patient/report metadata, the biomarkers that were tested, the biomarker
findings (including gene sequence variants), the surgical-pathology specimen findings,
and the clinical information — and it does so **agentically**:

1. **Extract** every entity from the document.
2. **Link** those entities into the nested schema (a finding belongs to a biomarker;
   a stage belongs to a specimen; a variant detail belongs to a finding).
3. **Verify** the result with deterministic checks plus evidence-grounded LLM checks.
4. **Self-correct** within a bounded budget (the ping-back / repair loop).
5. **Escalate to a human (SME)** *only* where the agents genuinely cannot decide —
   never as a dumping ground for noise.

The **north star**: maximise recall and correctness while keeping the SME queue small
and every value traceable back to the exact block of source text it came from.

## 2. The pipeline in one picture

```
PDF
 │
 ▼
preprocess ──► planner ──► teams ──► linker ──► verifiers ──► triage ──┐
(DocAI,        (which     (5 spec-  (assemble  (schema +     (classify  │
 blocks,        teams      ialist    nested     coverage +    defects)   │
 profiles,      run)       teams)    JSON +     recall +                 │
 NER,                                cross-     attribution +            │
 geometry)                           links)     binding V1–V4)           │
                                                                         │
            ┌──────────────── repair ◄───── (route = "repair") ◄─────────┤
            │   (re-extract / reprofile / re-link / renormalize /        │
            │    drop_and_flag) — then back to the linker (the cycle)    │
            ▼                                                            │
          linker … (loop until clean OR budget/recurrence guard trips)   │
                                                                         │
                          (route = "done") ─────────────────────────────┘
                                   │
                                   ▼
                                 VMAW ──► decision_router ──► persist
                          (deep-resolve     (auto_accept /     (write
                           escalations:      partial_accept /   artifacts +
                           EC / CITE / VA)   sme_flag)          SME queue)
```

The straight-line version of this (no triage/repair/VMAW) is **graph_linear**. The full
self-correcting version with the loop is **graph_selfcorrecting**. See
[architecture/overview.md](architecture/overview.md) for the node-by-node walk-through,
the state object, and the agent-communication detail.

## 3. Repository layout (where everything lives)

| Path | What it is |
|---|---|
| `core/` | Engine plumbing: schema loader, prompt renderer, persistence, GCS client, checkpointer, tool registry, state definition, env/observability. |
| `preprocess/` | PDF → blocks: DocAI parser, block profiler, medical NER, HGNC resolver, HGVS validator, negation, normalizers, fax-header filter, word geometry, bbox synthesis. |
| `agents/` | The reasoning agents: extractor, coverage auditor, arbiter, planner, linker, link-&-binding verifier, link registry, LLM adjudicators, normalizer hooks. |
| `teams/` | The specialist-team skeleton: `section_team.py` (factory) + `metadata_team.py`. |
| `pipeline/` | Orchestration: `graph_v1.py`, `graph_linear.py`, `graph_selfcorrecting.py`, `triage.py`, `repair.py`, `vmaw.py`, `agent_trace.py`, `link_metrics.py`, `runner.py`. |
| `verification/` | Deterministic + grounded verifiers: schema, phase-2 verifiers (coverage/link/evidence), recall floor, attribution, normalization. |
| `decision/` | `decision_router.py` — picks the final verdict. |
| `transform/` | `to_production.py` — converts the internal envelope to the production schema. |
| `ui/` | Streamlit review app (`ui/phase1/`): block explorer, entity browser, production browser, SME review, agent-trace timeline. |
| `config/` | All declarative config: schemas, prompts, team registry, tool registry, the typed registries (links / attribution / normalizer / NER / recall-floor / `dedup_policy.yaml` / `section_layout.yaml` / `link_registry_v4.yaml` / `teams_v4.yaml` / `production_mapping.yaml`), production mapping, seed data. See [reference/traceability.md](reference/traceability.md). |
| `scripts/` | Operational tooling (run, score, snapshot, to-production, env-check). See [reference/scripts.md](reference/scripts.md). |
| `scripts/gates/` | The deterministic verification gates — the regression suite. See [reference/traceability.md](reference/traceability.md). |
| `ground_truth/` | Dev/dummy ground-truth fixtures used for scoring. |
| `docs/` | This documentation. |

## 4. How to run it

```bash
make setup                 # one-shot: venv + deps + environment check
make verify                # local environment check (no cloud calls)
make run-local PDF=/path/to/report.pdf PHASE=3     # full self-correcting run (graph_selfcorrecting)
make verify-phase2         # run every scripts/gates/gate_p2_*.py
make verify-phase3         # run every scripts/gates/gate_p3_*.py
```

`PHASE=2` runs the straight-line graph (no triage/repair/VMAW, no SME queue). `PHASE=3`
runs the full loop and emits the SME queue, agent trace, and production output.
`PHASE=4` is the current target for v4 extraction — same self-correcting graph, wired
against the v4 schema/team/link/NER registries (`--version v4`). Outputs land under
`local_runs/artifacts/<doc_id>/` (gitignored — may contain PHI on real docs).

## 5. Key operating policies (do not violate)

- **Temperature is locked at 0.0** everywhere for determinism.
- **`GOOGLE_GENAI_USE_VERTEXAI=true`** so model calls go through the BAA-covered path;
  never push PHI; the repo is private; debug dumps and `local_runs/` stay local-only.
- **LLM adjudicators are gated** by the `LLM_ADJUDICATORS` env flag. With it off, the
  graph runs deterministic-only and any ungrounded LLM-only decision becomes an
  `uncertain` verdict that escalates (it is never silently confirmed).
- **VMAW never silently overrides a value.** A *contested* value adjudication goes to
  the SME; only grounded confirmations auto-apply.
- **The SME queue is for genuine ambiguity only.** Clean tokens that simply are not the
  thing being looked for are dropped-and-logged, not queued.

## 6. Reading order

1. **[architecture/overview.md](architecture/overview.md)** — the flow, the state, the
   graphs (v1/v2/v3), agent communication, and the ping-back/repair loop in full.
   - **[architecture/diagrams.md](architecture/diagrams.md)** — rendered flowcharts
     (PNG) for the overall pipeline and each block.
   - **[architecture/loops.md](architecture/loops.md)** — the agentic loop and the
     ping-back / repair loop, drawn with their exact branch conditions.
2. **[architecture/schema.md](architecture/schema.md)** — the v3/v4 schema, the biomarker
   two-level model, `variant_detail`, the `provenance` contract, and production mapping.
3. **[architecture/components.md](architecture/components.md)** — every component and
   every check, one by one.
4. **[reference/traceability.md](reference/traceability.md)** — the master mapping:
   milestone → gate → source files → prompt/config → the doc section that explains it.
5. **[reference/scripts.md](reference/scripts.md)** — operational scripts catalogue.
6. **[evaluation/README.md](evaluation/README.md)** — the eval module: how the extractor's
   output is graded against SME ground truth into a color-coded review workbook.
   - **[evaluation/architecture.md](evaluation/architecture.md)** — data flow, matcher,
     identity resolution, and the substring doc-id lookup.
   - **[evaluation/usage.md](evaluation/usage.md)** — CLI reference, common workflows,
     output artifacts, and troubleshooting.
   - **[evaluation/field_map.md](evaluation/field_map.md)** — the `field_map.yaml`
     contract that all eval consumers share.

## 7. What's after the pipeline — the eval module

Once the pipeline writes `extraction_production.json`, the **evaluation module**
(`evaluation/`) grades it against an SME-authored ground-truth XLSX and produces
a color-coded review workbook with per-cell verdicts (Match / Mismatch / Missed / Extra)
and precision/recall/F1 metrics per field, per section, and overall. Three modes:

```bash
python -m evaluation --doc <doc_id>                       # single doc
python -m evaluation --batch                              # every GT ↔ extraction pair
python -m evaluation --workbook path/to/multi_sheet.xlsx  # multi-sheet GT → consolidated colored review
```

See [evaluation/README.md](evaluation/README.md) for the full story.
