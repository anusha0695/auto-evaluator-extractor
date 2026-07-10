# Components & Checks — Every Piece, One by One

This walks every component in the codebase: what it does, where it lives, the prompt or
config that drives it, and — for the verifiers — exactly what it checks. Use it with
[overview.md](overview.md) (the flow) and [reference/traceability.md](../reference/traceability.md)
(which gate locks each piece).

---

## 1. Core engine (`core/`)

| File | Responsibility |
|---|---|
| `schema_loader.py` | Generates Pydantic models from `config/schemas/genomic_pathology_v4.json` at runtime; section-flexible (v2 / v3 / v4). Single source of truth for shapes. |
| `prompt_renderer.py` | Renders Jinja prompts — composes a team prompt with the base `system/` role prompts. |
| `state.py` | The shared graph-state definition + the section literals. |
| `persistence.py` | Reads/writes run artifacts to the configured backend (local dir; GCS optional). `load_storage_config("phase_1")` etc. |
| `gcs_client.py` | GCS access for the cloud backend (BAA path). |
| `checkpointer.py` | LangGraph checkpointer wiring. |
| `tool_registry.py` | Registers the agent tools listed in `config/tools.yaml`. |
| `prompt_renderer` + `env_loader` + `observability` | Env loading and OpenTelemetry-style tracing decorators. |
| `errors.py` | Typed errors (e.g. `AgentError`). |

## 2. Preprocessing (`preprocess/`)

PDF → the block layer everything else reads.

| File | Responsibility |
|---|---|
| `docai_parser.py` | Google DocAI Layout parse → blocks with bounding boxes, table structure (row/col/span), cross-page stitching. |
| `bbox_synthesizer.py` | Synthesises bounding boxes where DocAI didn't provide them. |
| `word_geometry.py` | Per-word geometry so the UI can highlight an exact surface inside a block. |
| `block_profiler.py` | Classifies each block: `text_role` (incl. narrative roles — `final_diagnosis`, `gross_description`, `microscopic_description`, `synoptic_report`, `clinical_history`, plus `fax_transport_noise`) and `target_umbrella_hints` (which section(s) it routes to). Batched at ~300 blocks/Gemini call. Prompt: `config/prompts/preprocess/block_profiler.j2`. |
| `medical_ner.py` | The deterministic NER floor → `parser_hypothesis` candidates (the recall baseline the Coverage Auditor checks against). Mapping: `config/ner_mapping.yaml`. |
| `hgnc_resolver.py` | Gene-symbol resolution (exact → use; fuzzy/context → flag `needs_review`; unresolved → emit + flag). Seed: `config/data/hgnc_aliases.tsv`. |
| `hgvs_validate.py` | Tiered, offline HGVS validation → the canonical `hgvs_normalized`. |
| `negation.py` | medspaCy-style ConText assertion (negated / uncertain / affirmed). |
| `normalizers.py` | Date / method / biomarker normalizers (verbatim preserved, canonical separate). |
| `fax_header_filter.py` | Drops fax-transport noise lines. |
| `preprocess_node.py` | The LangGraph node that orchestrates the above and bundles a self-contained `source.pdf` copy into the artifact folder. |

## 3. Agents (`agents/`)

| File | Responsibility |
|---|---|
| `base.py` | Shared agent base (model binding, ReAct plumbing). |
| `extractor.py` | The ReAct extractor: reasons, calls tools, emits Final-Answer JSON validated against the section model. Optional flag-gated `with_structured_output(method="json_schema")` finalizer (`EXTRACTOR_STRUCTURED=1`) with a free-text fallback; default is the free-text parse. Writes failing payloads to `local_runs/_extractor_debug/` for diagnosis. Base prompt: `config/prompts/system/extractor.j2`. |
| `coverage_auditor.py` | Compares extractor output to the `parser_hypothesis`; raises `gap_signal` + lists missed/spurious fields. Prompt: `config/prompts/system/coverage_auditor.j2`. |
| `arbiter.py` | Resolves Extractor↔Auditor disputes; emits per-field re-extract hints. Prompt: `config/prompts/system/arbiter.j2`. |
| `planner.py` | Deterministic planner — picks `active_team_keys` from document signals (skips teams with no relevant blocks). |
| `linker.py` | Assembles the nested envelope; forms cross-section links over the typed registry; supersession detection; accepts `relink_hints` from the repair loop. Also runs **intra-section dedup** via `_apply_intra_section_dedup` (with `_canon_change` + `_HGVS_PREFIX_RE`) — the field-agnostic reconciler that collapses multiple variant records at the same identity (e.g. KRAS p.G12D restated on two pages with different VAFs). Rules live in `config/dedup_policy.yaml` under `within_section:`. |
| `link_registry.py` + `config/link_registry_v4.yaml` | The typed link catalogue (Tier 1/2 cross-ref types; Tier-3 clinical links are config-toggleable). |
| `link_binding_verifier.py` | The four binding checks V1–V4 (see §5). |
| `adjudicators.py` | All the LLM hooks, lazily built and gated by `LLM_ADJUDICATORS`: `build_llm_adjudicators` (relationship/link/supersession/merge confirm), `build_attribution_fn`, `build_vmaw_hooks` (EC/CITE/VA), `build_recall_reread_fn`, `build_triage_llm`. |
| `normalizer_hooks.py` + `config/normalizer_map.yaml` | The renormalize adapter used by the repair loop. |

## 4. Teams (`teams/`)

- `section_team.py` — the **factory** that builds the 3-agent skeleton (Extractor →
  CoverageAuditor → Arbiter → re-extract) for any section, from `config/teams_v4.yaml`.
- `metadata_team.py` — the hand-written Phase-1 metadata team.
- The four active v4 teams and their sections (from `config/teams_v4.yaml`):
  `metadata_team`→`report_metadata`, `genomic_variant_team`→`Genomic_Variant_umbrella`,
  `molecular_biomarker_team`→`other_molecular_biomarker_umbrella`,
  `tested_biomarker_team`→`tested_biomarker_umbrella`.
  `specimen_findings_team`→`significant_findings` and
  `clinical_info_team`→`clinical_information` are carried non-destructively as
  `enabled: false` in v4.
- Team prompts: `config/prompts/<team>.j2`. In v4 the biomarker team's prompt is
  `molecular_biomarker_team_v4.j2` (flat biomarkers — no variant rules); the
  revived `genomic_variant_team.j2` owns `Genomic_Variant_umbrella` as a separate
  team and variants are routed AWAY from the biomarker team.
- Model assignments are per team (extractor/auditor = gemini-2.5-pro, arbiter =
  gemini-2.5-flash), temperature locked at 0.0, with a per-team `tool_allowlist`.

## 5. Verifiers (`verification/` + the binding verifier) {#verifiers}

The suite runs in `run_verifier_suite` (in `pipeline/graph_linear.py`). Each appends one
scorecard `{verifier_name, passed, field_errors/notes}`.

| Verifier | File | What it checks |
|---|---|---|
| **schema_validator** | `verification/schema_validator.py` | Envelope validates against the v4 Pydantic models; reports field-level errors with `loc`. |
| **CoverageVerifier** | `verification/core_verifiers.py` | Extracted coverage vs NER hypothesis count beyond gap tolerance. |
| **LinkConsistencyVerifier** | `verification/core_verifiers.py` | The links are internally consistent (endpoints exist, types valid). |
| **EvidenceConfidenceVerifier** | `verification/core_verifiers.py` | Confidence / grounding sanity per record. |
| **RecallFloorVerifier** | `verification/recall_floor.py` | Block-role recall floor: a block whose role implies a field (e.g. a `synoptic_report` block ⇒ a stage) but nothing extracted → a miss. Loud vs quiet strictness from `config/recall_floor.yaml`. An optional AI re-read (`build_recall_reread_fn`) can only *confirm* a miss — offline/no-text it keeps the miss (never silently clears). |
| **AttributionVerifier** | `verification/attribution.py` | Owner-keyed attribution: an attribute is anchored to the correct owner via the owner key (e.g. `specimen_id`), positional fallback when the key is null. Multiplicity is a scrutiny signal, not a gate. Map: `config/attribution_map.yaml`. |
| **NormalizationVerifier** | `verification/normalization.py` | The canonical/normalized value matches the verbatim; canonical written only when matched + different. Map: `config/normalizer_map.yaml`. |
| **HGVSValidityVerifier** | `verification/hgvs_validity.py` (`find_malformed_hgvs`) | HGVS structural-validity floor (V4-M2c/M6): every `coding_dna_change` / `protein_change` / `genomic_change` must be HGVS-syntactically valid (missing `c.`/`p.` prefix, garbled OCR → flag). |
| **LinkBindingVerifier V1–V4** | `agents/link_binding_verifier.py` | Evidence-grounded binding (below). |

### The four binding checks

- **V1 — component grounding (deterministic).** Each of name/method/result appears in a
  cited block. Fail → `refuted` (skip the LLM).
- **V2 — relationship confirm (LLM, gated).** Does the span state {method}→{result} for
  {name}, not a neighbour? Table-row binds are trusted and skip V2; only
  narrative/inferred binds spend the LLM. No adjudicator → `uncertain` (escalate).
- **V3 — orphan / hallucination (deterministic).** Every emitted value is present in
  source (else `refuted` = possible hallucination); every located NER candidate is
  attached to a record. **The orphan check compares a candidate against the *whole*
  emitted record** (names, results, interpretations, every `variant_detail` value,
  occurrence surfaces) — not just biomarker names — so genuinely-extracted variant
  specifics are not false orphans. A *genuinely unattached* candidate escalates **only**
  when its source is illegible/garbled (OCR symbol-splatter) or it carries
  `needs_review`; a clean token that simply is not a biomarker (an MRN, a date) is
  **dropped-and-logged**, never queued. This is what keeps the SME queue small.
- **V4 — link confirm (LLM, gated).** Each cross-section link is supported by its
  evidence. Deterministic gene-key links are trusted; narrative links need the LLM or
  become `uncertain`.

Verdicts are `confirmed | refuted | uncertain`.

## 6. Triage & repair (`pipeline/triage.py`, `pipeline/repair.py`)

- **triage** turns scorecards + binding verdicts into typed **defects** and decides
  repair-vs-escalate per defect — full taxonomy table in
  [overview.md §6](overview.md#6-the-ping-back--repair-loop-the-auto-fix). It enforces
  the per-team / global budget caps and the recur-guard, dedupes the SME queue by
  `(kind, ref, section, detail)`, and (with adjudicators on) consults the triage router.
- **repair** (`RepairExecutor`) applies the catalog: `re_extract_team`, `reprofile_block`,
  `renormalize_field`, `re_link`, `drop_and_flag`, `escalate`. It updates
  `section_outputs`/`team_results`, writes a `repair_log` entry, bumps the budget, and
  clears `repair_requests`; then the graph edge returns control to the linker.

## 7. VMAW (`pipeline/vmaw.py`)

Deep-resolves escalations with EC / CITE / VA in a kind-specific order (`ROUTING`).
Auto-applies grounded confirmations that pass a deterministic re-check; holds contested
value picks for the SME; drops ungroundable "no-support" kinds (`binding_refuted`,
`link_cannot_form`) from the envelope while keeping the payload in the queue. Writes a
`vmaw_log`. Detail in [overview.md §7](overview.md#7-vmaw--deep-resolution-of-escalations).

## 8. Decision & transform

- `decision/decision_router.py` — picks `VerdictV3` (`auto_accept | partial_accept |
  sme_flag`): SME if a team flagged it / any verifier failed / confidence below the
  per-team `auto_accept_confidence_threshold`; partial if some sections are clean and
  others escalated; else auto-accept.
- `transform/to_production.py` — internal envelope → production schema (reshape, fold,
  filter). Includes `_apply_supersession_filter`, which reads `envelope["links"]` and
  drops variant records that have a `variant_superseded_by` link to a winner (the
  loser is removed from the production output; the internal envelope keeps both for
  audit). See [schema.md §6](schema.md#6-production-mapping-transformto_productionpy).

## 9. UI (`ui/`)

The **SME Review Platform** — a Flask server plus a single-page HTML/JS app.
Six files, no subdirectories. The Flask side is a pure data pipe from
`local_runs/artifacts/` to the browser; all rendering logic lives client-side.

| File | Role |
|---|---|
| `app.py` (Flask) | Serves `index.html` at `/`, static assets at `/<path>`, the doc list at `/api/docs` (scans `local_runs/artifacts/` for subdirs containing `extraction_v2.json` — no hardcoded list), and per-doc artifact files at `/artifacts/<doc_id>/<filename>` (`extraction_v2.json`, `agent_trace.json`, `escalation_queue.json`, etc.). Default port 8501, overrideable via `PORT`. |
| `index.html` | Single HTML page with two top-level views: `#dashboardView` — the Extraction Queue table with 4 KPI stats (Total / Accepted / Escalated / Pending), a filter dropdown, and a table of docs; and `#documentView` — per-doc review with a left sidebar (Escalations + SME Decision + Reviewer name), a section-tab bar, an extraction table with per-field checkboxes and actions (bulk-select supported), and a field-detail panel that appears when a row is clicked. |
| `app.js` | Main JS module (`window.app`). Fetches `/api/docs` on load and `/artifacts/<doc>/extraction_v2.json` + `/artifacts/<doc>/agent_trace.json` per doc. Key functions: `showDashboard`, `filterQueue`, `toggleSelectAll`, `renderFieldActions`, `renderFieldDetail`. |
| `agent-trace.js` | Renders the per-field agent timeline in the detail panel. |
| `pdf-viewer.js` | PDF viewer with page-highlight support. |
| `styles.css` | All styling. |

The per-doc view has four section tabs — `report_metadata`, `Genomic_Variant_umbrella`,
`other_molecular_biomarker_umbrella`, `tested_biomarker_umbrella` — mirroring the four
active v4 sections.

**SME workflow (all client-side):** queue → pick a doc → pick a section tab → click a
row → detail panel shows the field's provenance, evidence, and agent-trace timeline →
apply an action (accept / edit / reject) which the reviewer records against their
name. Nothing is server-side rendered; the Flask server never mutates state, it just
serves the artifact JSON the pipeline wrote.

### 9.1 Artifact source — local or GCS (runtime-configurable)

`ui/app.py` supports two data sources through the same URL contract
(`/api/docs` and `/artifacts/<doc>/<file>`). The browser is **source-agnostic**
— nothing in `app.js` / `index.html` / the other JS files changes when the
source is switched. Selection is one env-var toggle read at server startup:

| Env var | Purpose | Default |
|---|---|---|
| `GCS_ARTIFACTS_BUCKET` | Set = GCS mode; unset/empty = local mode | `""` (= local) |
| `GCS_ARTIFACTS_PREFIX` | Key prefix inside the bucket that contains per-doc folders | `artifacts/` |
| `GOOGLE_APPLICATION_CREDENTIALS` | Service-account JSON path for the GCS client | (uses ADC if unset — optional on GKE / Cloud Run with Workload Identity) |
| `PORT` | Server port | `8501` |

**Local mode (default).** When `GCS_ARTIFACTS_BUCKET` is unset, `list_docs()`
walks `local_runs/artifacts/` for subfolders that contain `extraction_v2.json`,
and `serve_artifact()` streams files from `local_runs/artifacts/<doc>/<file>`.
This is today's behavior — no code change for existing local dev.

**GCS mode.** When `GCS_ARTIFACTS_BUCKET` is set, `list_docs()` calls
`storage.Client().bucket(BUCKET).list_blobs(prefix=BUCKET_PREFIX)` and groups
blob names into `{doc_id}/{filename}` pairs. A doc appears in the queue when
its `extraction_v2.json` blob exists; `hasPdf` reflects presence of
`source.pdf`. Individual artifact fetches (`serve_artifact()`) route through
`_bucket().blob(...).download_as_bytes()` and stream the bytes back with the
same `_MIME_MAP` used by the local branch.

The GCS `Client()` is **lazy-initialized** the first time it's needed —
`google-cloud-storage` is not imported at process start when the toggle is
off, so an operator running locally without gcloud credentials can still boot
the server.

**Path-traversal defense** runs BEFORE either backend is touched: `doc_id`
may not contain `/` or `..`, and `filename` may not contain `..`. Requests
that fail these checks return `HTTP 400` without ever hitting GCS or the
filesystem.

**Optimization note — signed URLs for large files.** The GCS branch currently
buffers each blob into memory (`io.BytesIO(blob.download_as_bytes())`). Fine
for the small JSON artifacts; noisy for large `source.pdf` files. If PDFs get
big, generate a v4 signed URL and return a `302` redirect instead:

```python
if filename.endswith(".pdf"):
    url = blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=15),
        method="GET",
    )
    return redirect(url)
```

Not wired today because the current artifacts are small enough; the pattern
is here so the next maintainer knows where to add it if it becomes necessary.

**Getting artifacts INTO GCS** is a separate concern — the UI reads wherever
the operator points it. Two common patterns: (1) modify `core/persistence.py`
so the pipeline writes to GCS directly; (2) keep writing to local disk during
extraction and sync post-run with `gsutil -m rsync -r local_runs/artifacts/
gs://<bucket>/artifacts/`. The UI change is independent of either choice.

## 10. Configuration index

| File | Drives |
|---|---|
| `config/schemas/genomic_pathology_v4.json` | the schema (Pydantic generation + structured-output spec) |
| `config/teams_v4.yaml` | the v4 team registry: section binding, prompt template, models, tool allowlist, thresholds, `enabled` flag |
| `config/teams.yaml` | the legacy v3 team registry (retained non-destructively) |
| `config/tools.yaml` | the agent tool registry |
| `config/prompts/system/*.j2` | base agent role prompts (extractor / coverage_auditor / arbiter) |
| `config/prompts/<team>.j2` | per-team extraction rules |
| `config/prompts/preprocess/*.j2` | block_profiler + medical_ner prompts |
| `config/link_registry_v4.yaml` | typed cross-section link types (v4) |
| `config/link_registry.yaml` | typed cross-section link types (v3, retained) |
| `config/dedup_policy.yaml` | intra-section and cross-section dedup rules (owner-wins + within-section identity) |
| `config/section_layout.yaml` | per-section shape table (`record_array`, `gene_key_field`, `empty` placeholder) read by linker + scorer |
| `config/attribution_map.yaml` | owner-keyed attribution targets |
| `config/normalizer_map.yaml` | renormalize targets |
| `config/ner_mapping_v4.yaml` | NER → umbrella routing (v4) |
| `config/ner_mapping.yaml` | NER → umbrella routing (v3, retained) |
| `config/recall_floor.yaml` | block-role → expected-field recall rules (loud/quiet) |
| `config/production_mapping.yaml` | internal ref → production location |
| `config/storage.yaml` | persistence backend config |
| `config/data/{biomarker_synonyms,method_synonyms}.yaml`, `hgnc_aliases.tsv` | seed dictionaries |
