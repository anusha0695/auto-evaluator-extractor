# Traceability — Milestone ↔ Gate ↔ Source ↔ Doc

The single cross-reference table. Each **gate** in `scripts/gates/` is a deterministic,
offline regression check that locks one milestone's contract. The build protocol is:
*build additively behind a gate*. Run them with `make verify-phase2` / `make verify-phase3`
(each globs `scripts/gates/gate_p{2,3}_*.py` in order) or run one directly with
`PYTHONPATH=. python scripts/gates/<file>`.

Naming convention: `gate_p<phase>_m<milestone>_<slug>.py` — the `m<milestone>` tag is
preserved so it cross-references the milestone history.

---

## Phase 3 gates (the self-correcting pipeline)

| Milestone | Gate file (`scripts/gates/`) | Locks (source it covers) | Doc section |
|---|---|---|---|
| M1 | `gate_p3_m1_block_profiler_roles.py` | `preprocess/block_profiler.py`, `config/prompts/preprocess/block_profiler.j2` — narrative roles + v3 routing vocab | components §2 |
| M2 | `gate_p3_m2_extractor_block_index.py` | `agents/extractor.py` — per-team compact block index in the prompt | components §3 |
| M3 | `gate_p3_m3_recall_floor.py` | `verification/recall_floor.py`, `config/recall_floor.yaml` — block-role recall floor + memoized re-read | components §5 |
| M4 | `gate_p3_m4_partial_accept_router.py` | `decision/decision_router.py` — partial-accept + record-level review | components §8 |
| M5 | `gate_p3_m5_contextual_linking.py` | `agents/linker.py`, `agents/link_registry.py`, `config/link_registry.yaml` — typed contextual links | overview §4 |
| M6 | `gate_p3_m6_triage_repair_loop.py` | `pipeline/triage.py`, `pipeline/repair.py`, `pipeline/graph_selfcorrecting.py` — defect taxonomy, budget/recur guards, repair catalog, the loop; **+ V3 orphan gate + SME-queue dedup** | overview §6 |
| M7 | `gate_p3_m7_vmaw_resolution.py` | `pipeline/vmaw.py` — EC/CITE/VA, auto-apply grounded, hold contested | overview §7 |
| M8a | `gate_p3_m8a_token_geometry.py` | `preprocess/word_geometry.py`, entity highlight mapper | components §2 / §9 |
| M8b | `gate_p3_m8b_agent_trace.py` | `pipeline/agent_trace.py` — per-agent trace, ordered, PHI-safe | overview §3 |
| M8c | `gate_p3_m8c_field_timeline.py` | `ui/phase1/field_trace.py` — merged timeline + **linking phase** + plain/technical | components §9 |
| M8d | `gate_p3_m8d_review_model.py` | `ui/phase1/sme_decisions.py` — review model + decision apply | components §9 |
| M8e | `gate_p3_m8e_entity_explorer.py` | `ui/phase1/components/entity_explorer.py`, `ui/phase1/evidence.py` — entity payload (boxes/status/links/trace) + canvas + provenance array/map read | components §9 |
| M9 | `gate_p3_m9_production_conversion.py` | `transform/to_production.py`, `config/production_mapping.yaml` — reshape/fold/filter | schema §6 |
| M10b | `gate_p3_m10b_attribution_verifier.py` | `verification/attribution.py`, `config/attribution_map.yaml` — owner-keyed attribution | components §5 |
| M10d | `gate_p3_m10d_contested_metrics.py` | `pipeline/link_metrics.py` — per-type contested-rate metrics | components §6 |
| M10e | `gate_p3_m10e_normalization_floor.py` | `verification/normalization.py`, `agents/normalizer_hooks.py`, `config/normalizer_map.yaml` — renormalize | components §5 |

## V4 gates (mCODE genomic_pathology_extraction migration)

The v4 migration is **non-destructive** (Decision D5): it adds new files
(`config/schemas/genomic_pathology_v4.json`, `config/teams_v4.yaml`,
`config/prompts/molecular_biomarker_team_v4.j2`, `config/link_registry_v4.yaml`,
`ground_truth/demo_v4.json`) and version-gated branches, so every v2/v3 gate stays green.
Run a v4 end-to-end with `make run-local PDF=… PHASE=4` (selects `--version v4`). Full
migration log + locked decisions: [`docs/migration/genomic_pathology_v4_plan.md`](../migration/genomic_pathology_v4_plan.md).

| Milestone | Gate file (`scripts/gates/`) | Locks (source it covers) | Doc section |
|---|---|---|---|
| M1 | `gate_v4_m1_schema_sections.py` | `config/schemas/genomic_pathology_v4.json`, `core/section_toggle.py` — 4-section schema + enable/disable helper | schema |
| M2 | `gate_v4_m2_variant_team.py` | `config/prompts/genomic_variant_team.j2`, `config/teams_v4.yaml`, `config/ner_mapping.yaml`, `agents/normalizer_hooks.py` (HGNC), `verification/hgvs_validity.py` — revived variant team + HGNC/HGVS | components §5 |
| M3 | `gate_v4_m3_biomarker_flat.py` | `config/prompts/molecular_biomarker_team_v4.j2` — flat biomarker (variants routed away) | schema |
| M4 | `gate_v4_m4_section_toggle.py` | `core/section_toggle.py`, `pipeline/graph_linear.py`, `pipeline/runner.py`, `scripts/process_local.py` — honor `enabled:false` on the v4 run path | components §8 |
| M5 | `gate_v4_m5_linker.py` | `config/link_registry_v4.yaml`, `pipeline/graph_linear.py` (`link_registry_path`) — variant↔tested / biomarker↔tested; drop disabled links | overview §4 |
| M6 | `gate_v4_m6_verifier_configs.py` | `verification/hgvs_validity.py` (wired), `pipeline/triage.py` (`invalid_hgvs`→escalate), `pipeline/graph_linear.py` (`drop_disabled_section_errors`) — verifier suite retarget | components §5 |
| M7 | `gate_v4_m7_scoring.py` | `scripts/score_against_ground_truth.py` (Genomic_Variant_umbrella), `ground_truth/demo_v4.json`, `verification/schema_validator.py` (`disabled_sections`) — score 4 sections | schema §6 |
| M8 | `gate_v4_m8_production.py` | `transform/to_production.py` — flat-tolerant biomarkers + fold the separate variant section | schema §6 |
| M9 | `gate_v4_m9_ui.py` | `ui/phase1/views/extraction_v2_view.py`, `ui/phase1/views/overview.py` — render variants + flat biomarkers; hide disabled | components §9 |
| M10 | `gate_v4_m10_field_rules.py` | `config/prompts/system/extractor.j2` + v4 team prompts — `" | "` concat + VERBATIM/DERIVED/null/count discipline | schema |

## Phase 2 gates (extraction + linking + verifier suite + scoring)

| Milestone | Gate file (`scripts/gates/`) | Locks |
|---|---|---|
| M1 | `gate_p2_m1_sections_prompts.py` | all sections build + validate; all prompts render |
| M2 | `gate_p2_m2_normalization_tools.py` | normalization/negation tools + registration |
| M2.5 | `gate_p2_m25_table_coords_stitch.py` | DocAI table coords + cross-page stitch |
| M3 | `gate_p2_m3_team_wiring.py` | the 4 teams wire against v3; tools + prompts resolve |
| M4 | `gate_p2_m4_planner.py` | planner activates/skips teams by signal |
| M5 | `gate_p2_m5_linker.py` | assemble + gene-key links + supersession |
| M6 | `gate_p2_m6_verifiers.py` | deterministic verifiers + V1/V3 + V2 escalation |
| M7 | `gate_p2_m7_decision_router.py` | decide_v2 aggregates teams + verifiers + binds |
| M8 | `gate_p2_m8_graph_linear.py` | graph_linear helpers + node wiring (end-to-end) |
| M9 | `gate_p2_m9_ui_v2.py` | v2 artifacts persisted + views + tabs wired |
| M10 | `gate_p2_m10_scoring.py` | all 2a sections scored (set/array/alias/flatten/object) |
| 2b | `gate_p2_2b_specimen_findings_team.py` | SpecimenFindingsTeam wired + planner-gated + scored |
| — | `gate_p2_adjudicators.py` | the 5 LLM hooks: parse, degrade-to-escalate, wiring |
| — | `gate_p2_denoise_floor.py` | NER floor denoise + duplication-stop prompt rules |
| — | `gate_p2_production_parity.py` | production gaps closed + scored |

> Phase 1 gate (`scripts/verify_environment.py`) is an environment check (deps / cloud
> pings), not a milestone gate — it stays in `scripts/`, not `scripts/gates/`.

## Prompts — where they live

| Prompt | Path | Used by |
|---|---|---|
| Extractor base | `config/prompts/system/extractor.j2` | every team's Extractor (via `{% include %}`) |
| Coverage Auditor base | `config/prompts/system/coverage_auditor.j2` | every team's CoverageAuditor |
| Arbiter base | `config/prompts/system/arbiter.j2` | every team's Arbiter |
| Metadata team | `config/prompts/metadata_team.j2` | metadata_team |
| Molecular biomarker team | `config/prompts/molecular_biomarker_team.j2` | molecular_biomarker_team (incl. variant rules) |
| Tested biomarker team | `config/prompts/tested_biomarker_team.j2` | tested_biomarker_team |
| Specimen findings team | `config/prompts/specimen_findings_team.j2` | specimen_findings_team |
| Clinical info team | `config/prompts/clinical_info_team.j2` | clinical_info_team |
| Block profiler | `config/prompts/preprocess/block_profiler.j2` | `preprocess/block_profiler.py` |
| Medical NER | `config/prompts/preprocess/medical_ner.j2` | `preprocess/medical_ner.py` |
| Genomic variant team (RETIRED) | `config/prompts/genomic_variant_team.j2` | none — merged into biomarker team |

## Typed registries / maps — where they live

| Concern | Config | Code |
|---|---|---|
| Cross-section link types | `config/link_registry.yaml` | `agents/link_registry.py`, `agents/linker.py` |
| Owner-keyed attribution | `config/attribution_map.yaml` | `verification/attribution.py` |
| Renormalization targets | `config/normalizer_map.yaml` | `agents/normalizer_hooks.py`, `verification/normalization.py`, `preprocess/normalizers.py` |
| NER → umbrella routing | `config/ner_mapping.yaml` | `preprocess/medical_ner.py` |
| Recall-floor rules | `config/recall_floor.yaml` | `verification/recall_floor.py` |
| Production mapping | `config/production_mapping.yaml` | `transform/to_production.py` |
| Team registry | `config/teams.yaml` | `teams/section_team.py` |
| Tool registry | `config/tools.yaml` | `core/tool_registry.py` |
| Seed dictionaries | `config/data/{biomarker_synonyms,method_synonyms}.yaml`, `hgnc_aliases.tsv` | `preprocess/{normalizers,hgnc_resolver}.py` |
