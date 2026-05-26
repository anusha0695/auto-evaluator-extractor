# Components & Checks — Every Piece, One by One

This walks every component in the codebase: what it does, where it lives, the prompt or
config that drives it, and — for the verifiers — exactly what it checks. Use it with
[overview.md](overview.md) (the flow) and [reference/traceability.md](../reference/traceability.md)
(which gate locks each piece).

---

## 1. Core engine (`core/`)

| File | Responsibility |
|---|---|
| `schema_loader.py` | Generates Pydantic models from `config/schemas/genomic_pathology_v3.json` at runtime; section-flexible (v2 or v3). Single source of truth for shapes. |
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
| `coverage_auditor.py` | Compares extractor output to the `parser_hypothesis`; raises `gap_signal` + lists missed/spurious fields. Prompt: `system/coverage_auditor.j2`. |
| `arbiter.py` | Resolves Extractor↔Auditor disputes; emits per-field re-extract hints. Prompt: `system/arbiter.j2`. |
| `planner.py` | Deterministic planner — picks `active_team_keys` from document signals (skips teams with no relevant blocks). |
| `linker.py` | Assembles the nested envelope; forms cross-section links over the typed registry; supersession detection; accepts `relink_hints` from the repair loop. |
| `link_registry.py` + `config/link_registry.yaml` | The typed link catalogue (Tier 1/2 cross-ref types; Tier-3 clinical links are config-toggleable). |
| `link_binding_verifier.py` | The four binding checks V1–V4 (see §5). |
| `adjudicators.py` | All the LLM hooks, lazily built and gated by `LLM_ADJUDICATORS`: `build_llm_adjudicators` (relationship/link/supersession/merge confirm), `build_attribution_fn`, `build_vmaw_hooks` (EC/CITE/VA), `build_recall_reread_fn`, `build_triage_llm`. |
| `normalizer_hooks.py` + `config/normalizer_map.yaml` | The renormalize adapter used by the repair loop. |

## 4. Teams (`teams/`)

- `section_team.py` — the **factory** that builds the 3-agent skeleton (Extractor →
  CoverageAuditor → Arbiter → re-extract) for any section, from `config/teams.yaml`.
- `metadata_team.py` — the hand-written Phase-1 metadata team.
- The five active teams and their sections (from `config/teams.yaml`):
  `metadata_team`→`report_metadata`, `molecular_biomarker_team`→`other_molecular_biomarker_umbrella`,
  `tested_biomarker_team`→`tested_biomarker_umbrella`, `clinical_info_team`→`clinical_information`,
  `specimen_findings_team`→`significant_findings`.
- Team prompts: `config/prompts/<team>.j2`. (`genomic_variant_team.j2` is retired —
  variants merged into the biomarker team.)
- Model assignments are per team (extractor/auditor = gemini-2.5-pro, arbiter =
  gemini-2.5-flash), temperature locked at 0.0, with a per-team `tool_allowlist`.

## 5. Verifiers (`verification/` + the binding verifier) {#verifiers}

The suite runs in `run_verifier_suite` (in `pipeline/graph_linear.py`). Each appends one
scorecard `{verifier_name, passed, field_errors/notes}`.

| Verifier | File | What it checks |
|---|---|---|
| **schema_validator** | `verification/schema_validator.py` | Envelope validates against the v3 Pydantic models; reports field-level errors with `loc`. |
| **CoverageVerifier** | `verification/core_verifiers.py` | Extracted coverage vs NER hypothesis count beyond gap tolerance. |
| **LinkConsistencyVerifier** | `verification/core_verifiers.py` | The links are internally consistent (endpoints exist, types valid). |
| **EvidenceConfidenceVerifier** | `verification/core_verifiers.py` | Confidence / grounding sanity per record. |
| **RecallFloorVerifier** | `verification/recall_floor.py` | Block-role recall floor: a block whose role implies a field (e.g. a `synoptic_report` block ⇒ a stage) but nothing extracted → a miss. Loud vs quiet strictness from `config/recall_floor.yaml`. An optional AI re-read (`build_recall_reread_fn`) can only *confirm* a miss — offline/no-text it keeps the miss (never silently clears). |
| **AttributionVerifier** | `verification/attribution.py` | Owner-keyed attribution: an attribute is anchored to the correct owner via the owner key (e.g. `specimen_id`), positional fallback when the key is null. Multiplicity is a scrutiny signal, not a gate. Map: `config/attribution_map.yaml`. |
| **NormalizationVerifier** | `verification/normalization.py` | The canonical/normalized value matches the verbatim; canonical written only when matched + different. Map: `config/normalizer_map.yaml`. |
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
  filter). See [schema.md §6](schema.md#6-production-mapping-transformto_productionpy).

## 9. UI (`ui/phase1/`)

A Streamlit review app. Key pieces:

| File | Role |
|---|---|
| `app.py` | Entry point; tabs/views. |
| `data_layer.py` | Loads run artifacts (`load_artifact`, `load_extraction`). |
| `block_view.py` | The per-block view model (bbox + text + role + entities + sourced fields). |
| `evidence.py` | `enumerate_entities`, `field_rationale_map`, `iter_provenance`, `build_entity_payload` — turns the envelope into selectable, highlightable entities with their trace. |
| `field_trace.py` | `assemble_field_trace` — one ordered timeline per field across phases: **extraction → linking → verification (incl. binding) → repair → resolution (VMAW)**. The `linking` phase surfaces each linker relationship touching the selected field with its rationale. |
| `components/entity_explorer.py` | The canvas: page image + highlight boxes (translucent wash + a solid outline on the selected entity) + the collapsible trace panel. |
| `views/` | `overview`, `preprocessing_view`, `metadata_extraction_view`, `extraction_v2_view`, `entity_browser_view`, `production_browser_view`, `sme_review_view`. |
| `sme_decisions.py` | The SME review model + decision apply (immutable original, occurrences safe). |

## 10. Configuration index

| File | Drives |
|---|---|
| `config/schemas/genomic_pathology_v3.json` | the schema (Pydantic generation + structured-output spec) |
| `config/teams.yaml` | the 5 teams: section binding, prompt template, models, tool allowlist, thresholds |
| `config/tools.yaml` | the agent tool registry |
| `config/prompts/system/*.j2` | base agent role prompts (extractor / coverage_auditor / arbiter) |
| `config/prompts/<team>.j2` | per-team extraction rules |
| `config/prompts/preprocess/*.j2` | block_profiler + medical_ner prompts |
| `config/link_registry.yaml` | typed cross-section link types |
| `config/attribution_map.yaml` | owner-keyed attribution targets |
| `config/normalizer_map.yaml` | renormalize targets |
| `config/ner_mapping.yaml` | NER → umbrella routing |
| `config/recall_floor.yaml` | block-role → expected-field recall rules (loud/quiet) |
| `config/production_mapping.yaml` | internal ref → production location |
| `config/storage.yaml` | persistence backend config |
| `config/data/{biomarker_synonyms,method_synonyms}.yaml`, `hgnc_aliases.tsv` | seed dictionaries |
