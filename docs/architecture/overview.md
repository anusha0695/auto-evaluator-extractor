# Architecture — Flow, Graphs, State, and the Self-Correction Loop

This is the end-to-end mechanics of the system: the orchestration graphs, the shared
state object, how the agents talk to each other inside a team, and exactly how the
ping-back / repair loop and VMAW work. Component internals (each agent, each verifier)
are in [components.md](components.md); the data shapes are in [schema.md](schema.md).

---

## 1. Three graphs (LangGraph)

The pipeline is a LangGraph `StateGraph`. There are three compiled graphs, each a
superset of the previous, selected by `PHASE` in `make run-local`:

| Graph | File | Nodes | Purpose |
|---|---|---|---|
| **v1** | `pipeline/graph_v1.py` | document_received → preprocess → metadata_team → decision_router → persist | Phase-1 baseline: metadata only, single team, straight line. Frozen. |
| **v2** | `pipeline/graph_linear.py` | … → planner → teams → linker → verifiers → decision_router → persist | Phase-2: all 5 specialist teams, contextual linking, the verifier suite. **Straight line — no self-correction, no SME queue.** |
| **v3** | `pipeline/graph_selfcorrecting.py` | v2 + **triage → repair (loop)** and **VMAW** | Phase-3: the self-correcting graph. This is the real product. |

### graph_selfcorrecting topology (the real flow)

```
document_received → preprocess → planner → teams → linker → verifiers → triage
                                                      ▲                    │
                                                      │   route="repair"   │
                                                      └──── repair ◄───────┤
                                                                           │ route="done"
                                                                           ▼
                                                                          vmaw
                                                                           │
                                                                           ▼
                                                                    decision_router → persist
```

The single conditional edge is at **triage**: if there are addressable defects it routes
to `repair` (which loops back to the `linker` and re-verifies); when nothing is
addressable it routes to `vmaw`. A recursion-limit backstop (`GRAPH_RECURSION_LIMIT`)
guarantees termination even if the budget logic were ever misconfigured.

> **Rendered flowcharts (PNG) for the overall pipeline and each block are in
> [diagrams.md](diagrams.md).** The two loops — the in-team **agentic loop** (§3) and
> the **ping-back / repair loop** (§6) — are drawn with their exact branch conditions in
> [loops.md](loops.md).

## 2. The shared state

Every node reads and returns a slice of one dict-shaped state (`core/state.py`). The
load-bearing keys:

| Key | Written by | Meaning |
|---|---|---|
| `doc_id`, source refs | document_received / preprocess | identity + where the PDF/artifacts live |
| `doc_profile` (`blocks`, `block_profiles`) | preprocess | the layout blocks + their role/umbrella classification |
| `parser_hypothesis` | preprocess (NER) | candidate entities found by the deterministic NER floor |
| `active_team_keys` | planner | which of the 5 teams to run for this document |
| `section_outputs` / `team_results` | teams | each team's extracted section + its agent results |
| `extraction` | linker | the assembled nested envelope (all sections joined) |
| `links` | linker | the cross-section relationships (typed, from the registry) |
| `verifier_scorecards` | verifiers | one scorecard per verifier (passed + field_errors/notes) |
| `binding_verifier` / `binding_items` | verifiers | V1–V4 binding verdicts (counts + per-item refs) |
| `repair_requests` | triage | the repair actions to apply this cycle |
| `defect_signatures_seen` | triage | recur-guard memory (a signature seen twice → escalate) |
| `repair_budget_used` | repair | repair-cycle accounting against the budget caps |
| `escalation_queue` | triage / vmaw / repair | the SME queue (deduped by `(kind, ref, section, detail)`) |
| `agent_trace` | teams | per-agent decision records for the UI timeline |
| `verdict` | decision_router | `auto_accept` \| `partial_accept` \| `sme_flag` |

## 3. Inside a team — the 3-agent ReAct skeleton

Every specialist team is the **same three-agent skeleton** instantiated with a
team-specific prompt and schema section (`teams/section_team.py`; `metadata_team.py` is
the hand-written Phase-1 instance). The agents communicate in sequence:

```
Extractor ──► CoverageAuditor ──► Arbiter ──► Extractor (re-extract)
  (ReAct        (did we miss /      (resolve      (re-run with the
   loop)         over-extract       the dispute,   arbiter's focused
                 vs the NER         emit re-       per-field hints)
                 hypothesis?)       extract hints)
```

- **Extractor** runs a manual **ReAct loop** — it reasons, calls tools (page loader,
  text search, DocAI lookup, the normalizers), and emits a **Final-Answer JSON** that is
  validated against the section's Pydantic model. (There is an optional, flag-gated
  structured-output finalizer; default is the free-text parse with a Pydantic check.)
- **CoverageAuditor** compares the extractor's output against the `parser_hypothesis`
  candidate count. If the gap exceeds `coverage_gap_tolerance` (0.10) it raises a
  `gap_signal` and lists the specific missed/spurious fields with reasons.
- **Arbiter** resolves the Extractor↔Auditor disagreement and, if needed, emits
  **per-field re-extract hints** ("look in the ancillary studies row").
- **Extractor (re-extract)** re-runs once with those focused hints.

Each step is recorded into `agent_trace` (`pipeline/agent_trace.py`) as a PHI-safe,
structural record (counts, verdicts, short reasoning snippets, per-field reasons) — this
is what the UI renders as the field timeline.

## 4. Linking

The **linker** (`agents/linker.py`) assembles the per-team section outputs into the one
nested `extraction` envelope and forms the **cross-section relationships** over the typed
registry (`config/link_registry.yaml`): e.g. a biomarker finding ↔ the panel it was
tested on (`variant_on_panel`), a finding ↔ its specimen, a stage ↔ its specimen. It
also detects supersession (an amended/addendum value supersedes the original, which is
preserved). Within-record relationships (a biomarker and its findings / variant_detail)
are synthesised structurally; the registry covers the cross-record ones. Deterministic
gene-key links are trusted; narrative/contextual links are confirmed by an LLM
adjudicator when `LLM_ADJUDICATORS` is on, else marked `uncertain` and escalated.

## 5. The verifier suite

`run_verifier_suite` (in `pipeline/graph_linear.py`) runs, in order, and appends one
scorecard each:

1. **schema_validator** — the envelope validates against the v3 Pydantic models.
2. **CoverageVerifier** — extracted coverage vs the NER hypothesis (gap tolerance).
3. **LinkConsistencyVerifier** — the links are internally consistent.
4. **EvidenceConfidenceVerifier** — confidence/grounding sanity.
5. **RecallFloorVerifier** — block-role recall floor: blocks whose role implies a field
   should exist but none was extracted → a miss (with an optional AI re-read that can
   only *confirm* a miss, never silently clear it).
6. **AttributionVerifier** — owner-keyed attribution: does this attribute belong to
   *this* owner (e.g. is this `tissue_type` really specimen[1]'s)?
7. **NormalizationVerifier** — the canonical/normalized form matches the verbatim.
8. **LinkBindingVerifier (V1–V4)** — the evidence-grounded binding checks (see below).

The binding verifier's four checks: **V1** component grounding (deterministic), **V2**
relationship confirm (does the span really state {method}→{result} for {name}?), **V3**
orphan/hallucination (every emitted value present in source; located candidates attached
to a record), **V4** cross-section link confirm. V2/V4 use an LLM when adjudicators are
on; otherwise they return `uncertain` (escalate, never silently confirm). Full detail in
[components.md](components.md#verifiers).

## 6. The ping-back / repair loop (the auto-fix)

This is the heart of the self-correction. After the verifiers, **triage**
(`pipeline/triage.py`) turns the scorecards + binding verdicts into typed **defects**,
then decides per defect: repair it, or escalate it.

### Defect taxonomy → repair action (`_ADDRESSABLE`)

| Defect | Repair action | Notes |
|---|---|---|
| `schema_error` | `re_extract_team` | needs a targetable team |
| `recall_miss_present` | `re_extract_team` | a block-role recall miss |
| `block_misroute` | `reprofile_block` | block had the role but was routed to the wrong section → re-profile + re-extract |
| `missing_provenance` | `re_extract_team` | a value with no occurrences/citation |
| `binding_refuted` | `re_extract_team` | ping back ONCE; recur-guard escalates on repeat |
| `link_cannot_form` | `re_link` | a V4 cross-section link refutation → re-link, don't re-extract |
| `normalization_invalid` | `renormalize_field` | canonical differs from verbatim → re-normalize |
| `attribution_contested` | `re_extract_team` | re-extract once → recur → VMAW (cite/va) → SME |
| `binding_uncertain` | **escalate-only** | needs human judgment |
| `needs_review` | **escalate-only** | OCR/inference flag set by an agent |

### Which agents get ping-back (and which don't)

- **Re-extraction** (`re_extract_team`, `reprofile_block`) pings the **owning team's
  Extractor**, which re-runs with the repair hint. Needs a targetable team
  (`_TEAM_REQUIRED`); if triage can't assign one, it escalates.
- **Re-link** (`re_link`) pings the **Linker** (via the repair→linker edge) with an
  *avoid / re-evaluate* hint — the extractor is not re-run.
- **Renormalize** (`renormalize_field`) runs the **normalizer hook** on the field; it
  writes the canonical form only when it matches and differs, and always preserves the
  verbatim.
- **drop_and_flag** removes an ungroundable record and queues it for SME (payload
  preserved for audit/restore).
- **escalate** sends the defect straight to the SME queue (→ VMAW first).

The Coverage Auditor / Arbiter do **not** have their own ping-back node — their
correction happens *inside* a team's re-extraction (they produce the hints the
re-extract consumes). Linking and normalization have dedicated repair actions because
they are post-extraction transforms.

### Termination guards

- **Per-team cap** and **global cap** (= number of active teams × 2) on repair cycles;
  once hit, remaining defects for that team escalate.
- **Recur-guard**: a defect *signature* `(team|ref|type)` that reappears after a repair
  was already attempted is escalated rather than repaired again (prevents loops).
- The agent **triage router** (`build_triage_llm`, when adjudicators are on) can also
  judge that an addressable defect is not re-extraction-fixable and route it straight to
  escalate; an invalid router action is ignored and the deterministic action stands.

After repair, control returns to the **linker** and the whole verify→triage step runs
again. The loop ends when triage finds nothing addressable (`route="done"`).

## 7. VMAW — deep resolution of escalations

On the `done` branch, **VMAW** (`pipeline/vmaw.py`, "Verify / Modify / Adjudicate /
Withhold") tries to deep-resolve each queued escalation before a human sees it, using
three capabilities tried in a kind-specific order (`ROUTING`):

- **EC — Expand Context**: re-read with a wider window / cross-references.
- **CITE — Evidence Citation**: produce the exact span that proves a claim.
- **VA — Value Adjudicator**: pick the canonical value when sources conflict.

Rules (locked): a **grounded confirmation** that passes a deterministic re-check is
auto-applied (e.g. CITE grounds an attribute→owner pairing → it is *confirmed*, not
overwritten). A **contested** VA pick (conflicting values, or overriding an existing
value) is **never** silently chosen — it is held for the SME. If VMAW cannot ground a
"no-support" kind (`binding_refuted`, `link_cannot_form`), the record is **dropped** from
the envelope and kept in the queue for audit/restore. Everything else stays flagged.

## 8. Decision + persist

The **decision_router** (`decision/decision_router.py`) picks the document verdict
(`VerdictV3`): `sme_flag` if a team flagged it or any verifier failed or confidence is
below threshold; `partial_accept` if some sections are clean and others escalated;
`auto_accept` otherwise. **persist** (graph_selfcorrecting) writes all artifacts to
`local_runs/artifacts/<doc_id>/` — the extraction envelope, `verification_v2.json`
(UI-shaped: links + scorecards), `agent_trace.json`, `repair_log.json`, `vmaw_log.json`,
`escalation_queue.json`, `link_metrics.json`, and auto-emits the production output
(`extraction_production.json`) on committed runs.

## 9. The LLM-adjudicator gate

`LLM_ADJUDICATORS` is a single env switch (`agents/adjudicators.llm_adjudicators_enabled`).
**Off** → fully deterministic: no LLM relationship/link confirmation, no LLM attribution,
no LLM triage routing; anything that *would* need an LLM judgment becomes `uncertain` and
escalates. **On** → the lazy Gemini hooks are built and injected:
`build_attribution_fn`, `build_vmaw_hooks` (EC/CITE/VA), `build_recall_reread_fn`,
`build_triage_llm`, and the link/relationship/supersession/merge adjudicators. This keeps
the offline gates safe (they run with stub deps) while production gets the full brain.
