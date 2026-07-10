# Pipeline Reference — what happens when a document is fed in

End-to-end trace of every file invoked, every agent that runs, every check it
performs, and how the artifacts and trace are recorded. Written against the v4
schema (`genomic_pathology_extraction`) and the graph_selfcorrecting flow that
`make run-local` uses for Phase 3/4.

---

## TL;DR — the 11-step flow

```
make run-local PDF=<path>
   ↓
pipeline/runner.py:run()                          ← entry, dispatches by pipeline_version
   ↓
pipeline/graph_selfcorrecting.py:build_…graph()   ← assembles the LangGraph state machine
   ↓
StateGraph executes the nodes in this order:

  document_received  →  preprocess  →  planner  →  teams  →  linker  →  verifiers
                                                                          │
                                                                          ▼
                                                                       triage
                                                                       ╱     ╲
                                                                 repair       vmaw
                                                                    │            │
                                                              (back to       decision_router
                                                               linker for         │
                                                               re-link +       persist
                                                               re-verify)         │
                                                                                END
```

**Important about the repair loop:** the `repair` node doesn't just hand off
to the linker — INSIDE the repair node, repair primitives like
`re_extract_team` and `reprofile_block` directly invoke the team's
**Extractor / CoverageAuditor / Arbiter** again (with focused hints) before
the graph edge `repair → linker` fires. `reprofile_block` also patches the
**BlockProfiler's** output first. So the actual chain when triage routes to
repair is:

```
triage → repair{ INVOKE team's Extractor/Auditor/Arbiter (re-extract)
                 OR patch BlockProfiler then re-extract
                 OR deterministically renormalize the field
                 OR drop the value
                 OR just log (for re_link) }
       → linker (re-assemble + re-dedup + re-cross-link)
       → verifiers (re-run all 8 + binding)
       → triage (decide again — loop or exit)
```

Every node returns a state-delta that the graph merges. The `agent_trace`
channel accumulates one record per agent invocation — that's the SME-facing
"who ran, what they decided, why" log.

---

## Entry — how `make run-local` reaches the graph

### `Makefile`

```make
run-local:
    @python -m pipeline.runner $(PDF)
```

### `pipeline/runner.py`

`run(pdf_path, pipeline_version="v4", …)` does five things:

1. **Resolve PDF.** Local path or `gs://` URI. Reads bytes into memory.
2. **Load configs.**
   - `config/schemas/genomic_pathology_v4.json` → `SchemaLoader` (builds Pydantic models per section at runtime).
   - `config/prompts/` → `PromptRenderer` (Jinja2 environment).
   - `config/teams.yaml`, `config/dedup_policy.yaml`, `config/ner_mapping.yaml`, `config/section_layout.yaml`, `config/link_registry_v4.yaml`.
3. **Build the LangGraph.** Calls `graph_selfcorrecting.build_selfcorrecting_graph(deps=…)` which assembles the StateGraph (see below).
4. **Seed the initial state.** `{doc_id, pipeline_version: "v4", pdf_bytes, agent_trace: [], …}`.
5. **Invoke.** `await graph.ainvoke(seed, config={"recursion_limit": 60})`.

On exit it returns a `RunResult` with `verdict`, `latency_ms`, `cost_usd`. The
graph itself is what writes every artifact to disk.

### `pipeline/graph_selfcorrecting.py:build_selfcorrecting_graph()`

Wires the 11 nodes and edges. Two key non-trivial bits:

```python
g.add_conditional_edges("triage", triage_route,
                        {"repair": "repair", "done": "vmaw"})
g.add_edge("repair", "linker")        # repair → re-link → re-verify → triage (LOOP)
g.add_edge("vmaw", "decision_router") # escalations resolved → router
```

The repair-loop is what makes this "self-correcting" — when triage finds an
auto-fixable defect, it routes back to repair → linker → verifiers → triage
until no defects remain (subject to budget caps).

---

## Node 1 — `document_received`

**File**: `pipeline/graph_selfcorrecting.py:_document_received_v3_node()`

**What it does**: validates the seed state has the required fields, asserts
`pipeline_version == "v4"`, returns the initial scaffolding (`team_outputs={}`,
`verifier_scorecards=[]`, `latency_ms=0`, `cost_usd=0.0`).

**Why it exists**: gives every downstream node a clean place to merge deltas
into — no `KeyError`s when triage looks for `repair_budget_used` and the field
doesn't exist yet.

**Trace records emitted**: none (sanity-only).

---

## Node 2 — `preprocess`

**File**: `preprocess/preprocess_node.py:make_preprocess_node()`

This node runs **4 sub-agents in sequence**. Each emits one `agent_trace`
record (phase=`preprocess`).

### 2a. DocAIParser
- **File**: `preprocess/docai_parser.py`
- **What**: POSTs the PDF bytes to Google's Document AI Layout Parser, gets back
  per-page text + per-block bounding boxes + reading order.
- **Output**: `doc_profile.pages[]`, `doc_profile.blocks[]` with `block_id`,
  `bounding_box`, `text`, `page_number`.
- **Artifacts written**: `docai_raw_full.json`, `docai_raw_proto.pb`,
  `docai_raw_text.txt`, `docai_raw.json` (all under
  `local_runs/artifacts/<doc_id>/`).
- **Trace plain**: *"Our system parsed the PDF into pages, blocks, and text we
  can reason over."*

### 2b. FaxHeaderFilter
- **File**: `preprocess/fax_header_filter.py`
- **What**: scans every block for fax transmission noise (TO/FROM headers,
  banner pages, page-count footers). Flags matching blocks with
  `text_role: fax_transport_noise` so they're hidden from downstream prompts.
- **Output**: mutates `doc_profile.blocks[].text_role` in place.
- **Trace plain**: *"Our system flagged fax-transport noise (headers/banners)
  so it's ignored downstream."*

### 2c. BlockProfiler
- **File**: `preprocess/block_profiler.py`
- **What**: a Gemini Flash call that classifies every block by **role**
  (`header_metadata`, `result_table_cell`, `methodology`, `signature_block`,
  `disclaimer`, …) and **section hint** (which v4 umbrella the block likely
  contributes to: `report_metadata`, `Genomic_Variant_umbrella`,
  `other_molecular_biomarker_umbrella`, `tested_biomarker_umbrella`).
- **Batching**: 300 blocks per Gemini call, max 3 concurrent calls.
- **Output**: `doc_profile.block_profiles[]` — one `{block_id, text_role,
  target_umbrella_hints[]}` per block.
- **Why it matters**: the role drives the recall-floor verifier (a result table
  cell that came back empty is loud); the section hint scopes which blocks each
  team prompt sees.
- **Trace plain**: *"Our system labelled each block of text with a role and
  routed it to the right section(s)."*

### 2d. MedicalNER
- **File**: `preprocess/medical_ner.py`
- **What**: runs three SciSpaCy models (en_ner_bionlp13cg_md,
  en_ner_bc5cdr_md, en_core_web_sm) over each block to extract candidate
  entities (genes, dates, MRNs, drug names). A deterministic post-processor
  (`config/ner_mapping.yaml`) routes raw labels into the right v4 section:
  e.g. `Bio_molecule` + canonical gene → `genomic_variant_team` candidate.
- **Output**: `parser_hypothesis.candidates[]` — one
  `{label, target_umbrella, surface, block_id, page, occurrences[]}` per
  candidate, scoped per umbrella.
- **Why it matters**: gives each team a "seed list" of likely entities. The
  team prompts treat this as a RECALL FLOOR (must capture everything the seed
  finds, plus anything the seed missed).
- **Trace plain**: *"Our system spotted clinical entities (genes, dates, IDs)
  in the source text and proposed candidates for each section."*

### Preprocess artifacts written

```
local_runs/artifacts/<doc_id>/
   docai_raw.json          ← raw DocAI response
   pages.json              ← cleaned page-level text
   blocks.json             ← block bbox + text + text_role
   block_profiles.json     ← block_id → {text_role, target_umbrella_hints}
   parser_hypothesis.json  ← NER candidates per umbrella
   word_geometry.json      ← word-level bboxes (for UI highlighting)
   source.pdf              ← copy of the original PDF
```

---

## Node 3 — `planner`

**File**: `agents/planner.py:Planner.plan()` (wrapped by `_make_planner_node`)

**What it does**: decides which extraction teams to activate for THIS document.
Deterministic. Looks at:
- `teams.yaml` enabled flags (v4 disables `significant_findings`,
  `clinical_information`).
- `block_profiles[].target_umbrella_hints` — if no block hints at a section,
  the team can be skipped to save cost.

**Output**: `plan.active_team_keys = ["metadata_team", "genomic_variant_team",
"molecular_biomarker_team", "tested_biomarker_team"]` (for v4).

**Trace record**:
```
phase: planning
agent: Planner
plain: "Our system decided which extraction teams should run for this document."
output_summary: "active=[…] skipped=[…]"
reasoning: <plan.rationale — short string explaining why each team is in/out>
```

---

## Node 4 — `teams` (the heavy lifter)

**File**: `pipeline/graph_linear.py:_make_teams_node()` — runs every active
team in parallel via `asyncio.gather()`.

Each team is a `teams/section_team.py:SectionTeam`. A team is a 4-agent ReAct
loop: **Extractor → CoverageAuditor → Arbiter → (optional) Extractor (re-extract)**.

### 4a. Extractor (`agents/extractor.py:Extractor`)

The main extraction agent. Implementation details:

**Prompt** (`config/prompts/system/extractor.j2` + the team's overlay):
- Field contract rendered from the team's schema section (every required
  field, type, format, pattern).
- Recall mandate ("the parser hypothesis is a FLOOR, not a ceiling").
- Extraction-mode contract: VERBATIM / DERIVED / VERBATIM_OR_INFERRED.
- Multi-fragment concatenation rule (`" | "` separator).
- **Reasoning trace requirement** — must emit `<reasoning>…</reasoning>` block
  before the final JSON (recently added so the SME sees the model's actual
  thought process even on single-shot extractions).
- Output format: raw JSON, no markdown fences, no preamble.

**Loop** (up to `MAX_ITERATIONS=12`, force-final on iterations 11/12):
```
while iter < MAX:
    response = await llm.ainvoke(messages + tool-call results)
    if response.has_final_answer:
        payload = parse_json(response.content)         # handles <reasoning>…JSON shape
        if payload has section_name as sole top-level key:
            payload = payload[section_name]            # unwrap (recent fix)
        _coerce_scalar_tics(payload)                   # e.g. page_number: [1] → 1
        return schema_validator.validate(payload)
    if response.has_tool_calls:
        for tc in response.tool_calls:
            result = await tool.invoke(tc.args)
            messages.append(ToolMessage(result))
```

**Tools the Extractor can call** (registered per team in `teams.yaml`):
- `state_read` — fetch a slice of state (e.g. block text by ID, page text).
- `hgnc_normalize` — canonicalize a gene symbol against the HGNC seed TSV.
- `hgvs_validate` — check a coding/protein change is structurally valid.
- `biomarker_normalize` — canonical biomarker name lookup.
- `date_parser` — parse a date string into ISO format.

**Validation pre-checks** (in `_parse_and_validate`):
1. JSON parse — if raw json.loads fails, fall back to a regex that pulls the
   first `{…}` object out of the text (tolerates the `<reasoning>` prefix).
2. Section-wrap unwrap — if payload has exactly one key equal to the section
   name, unwrap once.
3. Scalar coercion — `_coerce_scalar_tics` fixes a few known LLM type tics
   (singular `page_number` emitted as `[1]`, etc.).
4. Pydantic validation against the section's auto-generated model.

**On validation failure**: dump the raw content to
`local_runs/_extractor_debug/<section>_<ts>.txt` for inspection, inject a
HumanMessage with the validator's exact error, retry once. Two failures → raise
`AgentError`.

**Output** (`SectionTeamResult.extractor_result`):
```
{output: <validated section dict>,
 llm_confidence_score: 0.0–1.0,
 reasoning_trace: [{role, content, tool_calls}, …],
 latency_ms, tool_calls_made, llm_calls}
```

### 4b. CoverageAuditor (`agents/coverage_auditor.py`)

**Second pass to catch what the Extractor missed.** Separate Gemini call,
different prompt.

**Prompt** (`config/prompts/system/coverage_auditor.j2`):
- Receives: the extractor's output + the parser hypothesis (NER candidates) +
  the cleaned page text.
- Task: cross-reference. Flag (a) fields the extractor populated that don't
  appear supported by the text (`spurious_fields[]`), (b) entities the parser
  hypothesis found that the extractor IGNORED (`missed_fields[]`).

**Output**:
```
{coverage_ok: bool,
 gap_signal: bool,
 missed_fields: [{field_name, hypothesis_evidence, why_extractor_should_have_caught_it}],
 spurious_fields: [{field_name, evidence_against}],
 auditor_notes: "<one paragraph>",
 parser_hypothesis_misses: int}
```

**Why it matters**: independent recall check. When the extractor's confidence is
high but the auditor flags a gap, the Arbiter mediates.

### 4c. Arbiter (`agents/arbiter.py`) — fires only when Extractor & Auditor disagree

**Prompt** (`config/prompts/system/arbiter.j2`):
- Receives: both prior outputs + the gap evidence.
- Task: decide one of `ACCEPT_EXTRACTOR` (auditor was wrong), `RE_EXTRACT`
  (try again with focused hints), `INVOKE_VMAW` (escalate — Phase 3+).

**Output**:
```
{policy: "ACCEPT_EXTRACTOR" | "RE_EXTRACT" | "INVOKE_VMAW",
 re_extract_hints: [{field_name, hint: "look in block X"}, …],
 reasoning: "<one paragraph>"}
```

### 4d. Extractor (re-extract) — fires only when Arbiter says `RE_EXTRACT`

Same agent class as 4a, but invoked with a focused prompt that includes the
hints. Emits one more validated section dict. The team's final output is the
re-extract output (more focused, fewer misses).

### Trace records emitted by `teams` node (per team)

```
Extractor                         plain: "Our system read this section…"
Extractor · thought #1             plain: "Thought #1 — <first line of model text>"
Extractor · tool · hgnc_normalize  plain: "The model asked the hgnc_normalize tool…"
Extractor · hgnc_normalize → result plain: "The hgnc_normalize tool returned its answer…"
…
Extractor · final answer          (only if model went straight to JSON without <reasoning>)
CoverageAuditor                   plain: "A second check compared what was pulled out…"
Arbiter                           (only if gap was flagged) plain: "Because the two steps disagreed…"
Extractor (re-extract)            (only if arbiter said RE_EXTRACT) plain: "Our system took another careful pass…"
```

All four teams' traces are concatenated into the unified `agent_trace`.

---

## Node 5 — `linker`

**File**: `agents/linker.py:Linker` (wrapped by `_make_linker_node`)

This is where the per-team outputs become a single document envelope, with
cross-section links and dedup.

### What the linker does, in order

**Step 1 — Assemble**: walk `state.section_outputs` (team_key → section dict)
and produce the envelope:
```python
envelope = {
  "count_of_extracted_objects": <sum of section counts>,
  "report_metadata": {…},
  "Genomic_Variant_umbrella": {…},
  "other_molecular_biomarker_umbrella": {…},
  "tested_biomarker_umbrella": {…},
}
```

**Step 2 — Cross-section linking** (registry-driven, `config/link_registry_v4.yaml`):

For each registered link type, walk both endpoint sections and try to match
records. v4 has 4 active types:

| Type | From section | To section | Rule |
|---|---|---|---|
| `variant_on_panel` | `Genomic_Variant_umbrella.Genomic_Variants[]` | `tested_biomarker_umbrella.tested_biomarkers[]` | Same HGNC-canonical gene |
| `tested_to_result` | `tested_biomarker_umbrella.tested_biomarkers[]` | `other_molecular_biomarker_umbrella.other_molecular_biomarkers[]` | Same HGNC-canonical gene |
| `variant_superseded_by` | `Genomic_Variant_umbrella.Genomic_Variants[]` | `Genomic_Variant_umbrella.Genomic_Variants[]` (self-typed) | Addendum block references the original |
| `superseded_by` | `other_molecular_biomarker_umbrella.other_molecular_biomarkers[]` | same section | Same idea, biomarker side |

For each emitted link the linker runs **4 deterministic validation checks**:
1. `type ∈ registry` (no unknown types).
2. Endpoint sections match the registered pair.
3. Both refs resolve in the envelope.
4. `evidence_block_ids` cited; `confidence ≥ floor`.

**Self-typed pair guard** (recent fix): for `variant_superseded_by`, `i ≠ j` so
a variant never links to itself.

**Step 3 — Dedup** (`config/dedup_policy.yaml`):

The linker runs **two dedup passes**:

*Cross-section (owner-wins).* When the same gene appears in BOTH
`Genomic_Variant_umbrella` and `other_molecular_biomarker_umbrella`, the
canonical owner wins. The policy says: a record with a structural
`coding_dna_change` / `protein_change` belongs to `Genomic_Variant_umbrella`;
if the biomarker team picked up the same gene, that record is dropped from
the biomarker section.

*Intra-section (identity-collapse).* Under `within_section:` the policy
declares an identity — for `Genomic_Variant_umbrella`,
`identity_keys: [gene_key, amino_acid_change]`. `_apply_intra_section_dedup`
walks each section and groups records by that identity:
- Same-identity records with **identical** non-identity fields → drop the
  duplicates.
- Same-identity records with **conflicting** non-identity fields → keep the
  winner (higher `page_number`) and emit a `variant_superseded_by` link from
  loser→winner into `envelope["links"]`. Downstream,
  `transform/to_production._apply_supersession_filter` reads those links and
  removes the superseded record from the production output (the internal
  envelope keeps both, for audit).

Each dedup drop emits a trace record:
```
phase: dedup
agent: Linker · Dedup · Genomic_Variant_umbrella owns
plain: "The same entity appeared in two sections — our system kept the canonical owner's record and dropped the duplicate from the lower-priority section."
```

**Step 4 — Supersession**: if an addendum block references an earlier finding,
mark the older record `superseded: true` and emit a `supersession_event`. Each
emits:
```
phase: supersession
agent: Linker · Supersession (resolved | needs_review)
plain: "An addendum block referred back to an earlier finding…"
```

**Step 5 — Optional LLM contextual linking** (Tier 2/3 — disabled by default in
this run): for ambiguous cases, an LLM adjudicator proposes a link, then the
same 4 deterministic checks decide whether to commit it. Rejected proposals
become `dropped_contextual_links` records (visible in the trace under
"Linker · contextual_dropped").

### Linker output

```
LinkResult(
  envelope=<the unified envelope>,
  links=[Link(from_ref, to_ref, type, method, confidence, rationale, evidence_block_ids), …],
  needs_review_refs=[<refs flagged for SME during assembly>],
  dedup_drops=[…],
  supersession_events=[…],
  dropped_contextual_links=[…]
)
```

### Artifact written

```
local_runs/artifacts/<doc_id>/extraction_v2.json
```

This is the canonical envelope, written EVERY run regardless of verdict.

---

## Node 6 — `verifiers`

**File**: `pipeline/graph_selfcorrecting.py:_make_selfcorrecting_verifier_node`
which calls `pipeline/graph_linear.py:run_verifier_suite()`.

**8 verifiers run in sequence**, each producing one `scorecard`. All are
**deterministic** except `LinkBindingVerifier` (Gemini-backed).

### 6a. `schema_validator`
- **Check**: every emitted envelope must Pydantic-validate against
  `genomic_pathology_v4`. STRUCTURAL errors (`type`, `required`,
  `additionalProperties`) are hard failures; COSMETIC nits (`pattern`,
  `format`, `min/max`) are advisory.
- **Why**: catches teams that broke the contract despite their own validate
  step (e.g. linker introduced an unknown key).

### 6b. `coverage_audit` (`verification/coverage.py`)
- **Check**: compare the count of populated records per section to the parser
  hypothesis count. If extractor count + tolerance < hypothesis count, flag a
  gap.
- **Why**: cross-doc recall floor (different from the per-team CoverageAuditor
  which is intra-team).

### 6c. `link_consistency` (`verification/link_consistency.py`)
- **Check**: every cross-section link's `from_ref` and `to_ref` must resolve
  to records that actually exist in the envelope.
- **Why**: catches dangling refs (linker bug or post-linker mutation).

### 6d. `evidence_confidence` (`verification/evidence_confidence.py`)
- **Check**: every leaf record must cite at least one block_id in its
  `evidence_block_ids`.
- **Why**: no ungrounded values pass downstream.

### 6e. `recall_floor` (`verification/recall_floor.py`)
- **Check**: cross-references `block_profiles[].text_role` with the envelope
  — if a block is roled `result_table_cell` but no record cites that block,
  this is a "miss". Loud misses trigger an AI re-read of just that block.
- **Why**: catches systematic recall gaps without re-reading the whole doc.
- **v4 nuance**: misses in disabled sections (significant_findings,
  clinical_information) are subtracted from the count.

### 6f. `attribution` (`verification/attribution.py`)
- **Check**: for nested attributes (e.g. `lymph_node_details.laterality`),
  does the attribute actually describe its owner record?
- **Why**: catches "this laterality belongs to specimen A, not specimen B"
  errors. Routes to repair/VMAW/SME when contested.

### 6g. `normalization` (`verification/normalization.py`)
- **Check**: for fields with a canonical form (gene symbols, dates, HGVS),
  does the extracted value match the canonical?
- **Why**: triggers `renormalize_field` repair when the canonical differs.

### 6h. `hgvs_validity` (`verification/hgvs.py`)
- **Check**: every coding / protein / genomic change is HGVS-syntactically
  valid.
- **Why**: catches "1849G>T" without the `c.` prefix, garbled OCR, etc.

### 6i. `LinkBindingVerifier` (`agents/link_binding_verifier.py`) — LLM-backed

For every link the linker emitted, run 4 binding checks:

| Check | Verifies | Method |
|---|---|---|
| V1 grounding | the value is supported by the cited text | LLM YES/NO |
| V2 relationship | this result belongs to this test (not a neighbour) | LLM YES/NO |
| V3 hallucination | this value actually appears in the report | LLM with multi-section haystack |
| V4 link | this cross-reference holds | LLM YES/NO |

Each check produces a `binding_item`:
```
{ref, check: "V1"…"V4", verdict: "supported" | "refuted" | "uncertain",
 evidence: "<short citation>"}
```

`refuted` / `uncertain` items flow to triage as defects.

### Verifier outputs

```
verifier_scorecards: [scorecard, …]   # one per verifier
binding_summary: {refuted: N, uncertain: M}
binding_items: [item, …]              # per-link verdicts
block_reads: {block_id: re-read result}  # memoized re-reads from recall_floor
```

### Trace records emitted

```
phase: verification
  agent: schema_validator        plain: "A structural check confirmed the envelope's shape…"
  agent: coverage_audit          plain: "A coverage check compared what was extracted against…"
  agent: link_consistency        plain: "A check made sure every cross-section link points…"
  agent: evidence_confidence     plain: "A check made sure each finding has at least one cited…"
  agent: recall_floor            plain: "A check looked for fields the block-role mapping says…"
  agent: attribution             plain: "A check made sure each attribute really describes…"
  agent: normalization           plain: "A check looked for fields whose canonical form…"
  agent: hgvs_validity           plain: "A check confirmed every HGVS change is structurally valid."
  agent: LinkBindingVerifier     plain: "A binding check confirmed the relationship each link claims…"
  agent: LinkBindingVerifier · V1/V2/V3/V4 (one per binding item)
```

### Artifact written

```
local_runs/artifacts/<doc_id>/verification_v2.json  # UI-shaped (links + scorecards + attribution metrics)
local_runs/artifacts/<doc_id>/verification_v3.json  # raw scorecards + binding_summary
```

---

## Node 7 — `triage` (the routing brain)

**File**: `pipeline/triage.py:TriageAgent.decide()` (wrapped by
`make_triage_node`)

**What it does**: looks at every failed scorecard + every refuted/uncertain
binding item and decides per-defect:
- **Auto-fixable** → emit a `repair_request` (one of 5 repair actions).
- **Not auto-fixable** → emit an `escalation` (queued for SME / VMAW).

### Defect taxonomy

| Defect | Source | Repair action |
|---|---|---|
| schema error in repairable field | schema_validator | `re_extract_team` |
| coverage gap | coverage_audit | `re_extract_team` |
| loud recall floor miss | recall_floor | `reprofile_block` (re-classify the block, re-extract) |
| canonical mismatch | normalization | `renormalize_field` |
| HGVS malformed | hgvs_validity | `renormalize_field` |
| link binding refuted | LinkBindingVerifier | `re_link` |
| ungroundable value | evidence_confidence | `drop_and_flag` |
| attribution contested | attribution | escalate to VMAW (V4: 2-pass — repair first) |
| dangling cross-ref | link_consistency | `re_link` |

### Termination guarantees

The triage→repair→linker→verifiers→triage loop could spin forever if defects
keep firing. Three guarantees:

1. **Per-team repair cap** (default 1) — each team gets at most one re-extract
   per run.
2. **Global budget** = `n_teams × factor` (configurable). Once spent, all
   remaining defects escalate.
3. **Recur guard** — if the SAME defect signature `(kind, ref, section)` is seen
   twice in a row, force-escalate (the repair isn't working).

### Route decision

```python
if repair_requests and budget_remaining and not recur_guard_armed:
    route = "repair"
else:
    route = "done"
```

The conditional edge `g.add_conditional_edges("triage", triage_route, …)`
routes to `repair` or `vmaw` accordingly.

### Trace records emitted

```
phase: triage
  agent: Triage              plain: "Our system classified the open defects and routed each one…"
                              output_summary: "route=repair; N repair(s), M escalation(s)"
  agent: Triage · repair · <action>  (one per repair_request)
                              plain: "Triage queued an automatic '<action>' repair for this field."
  agent: Triage · escalate · <kind>  (one per escalation)
                              plain: "Triage flagged this '<kind>' for a human reviewer."
```

---

## Node 8 — `repair` (only if triage routed here)

**File**: `pipeline/repair.py:RepairExecutor.apply()` (wrapped by
`make_repair_node`)

**Critical distinction — work happens at TWO levels:**

```
┌─────────────────────────────────────────────────────────────────┐
│  INSIDE the repair node — RepairExecutor.apply()                │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ For each repair_request:                                  │  │
│  │   re_extract_team   → team.run(repair_hints=…)            │  │
│  │                        ↑ THIS INVOKES THE EXTRACTOR       │  │
│  │                          (full SectionTeam pipeline:       │  │
│  │                           Extractor → Auditor → Arbiter)  │  │
│  │   reprofile_block   → patch block_profiles, then          │  │
│  │                        team.run() → INVOKES EXTRACTOR     │  │
│  │   renormalize_field → deterministic rewrite (no LLM)      │  │
│  │   re_link           → no work here; just logged           │  │
│  │   drop_and_flag     → surgical envelope edit (no LLM)     │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                                ↓
                Graph edge: repair → linker
                                ↓
        Re-link + re-verify + re-triage (the loop)
```

### Per-action detail (what really happens for each)

#### `re_extract_team` — Extractor IS reinvoked

In `_re_extract`:
```python
hints = [{"field_name": req.target_ref, "hint": f"{req.detail}. Candidate blocks: …"}]
res = await team.run(state, repair_hints=hints)   # FULL SectionTeam pipeline
section_outputs[res.schema_section] = res.output  # patched in place
```

The team's `run()` method invokes **Extractor → CoverageAuditor → Arbiter →
optional re-extract** — the SAME 4-agent flow as Node 4, just with focused
hints. So yes, the model is called again, but the call is scoped to ONE team
(not the whole document) and biased toward the flagged field.

#### `reprofile_block` — BlockProfiler patched THEN Extractor reinvoked

In `_reprofile`:
```python
# Step 1: surgical edit to the block_profiler output (no Gemini call)
for bp in doc_profile.block_profiles:
    if bp.block_id == flagged_block_id:
        bp.target_umbrella_hints.append(suggested_section)
# Step 2: now the team can see the block — re-extract
await self._re_extract(req, …)   # ← invokes Extractor with corrected routing
```

So `reprofile_block` = patch the routing, then `re_extract_team` is called
internally. Two-step within one repair primitive.

#### `renormalize_field` — fully deterministic, no LLM

In `_renormalize`:
```python
# detail = "{normalizer_key}: {input} → {canonical}"
key = req.detail.split(":")[0].strip()        # e.g. "hgnc_normalize"
tool = self._normalizers.get(key)             # the deterministic resolver
tool.apply(section_outputs, req.target_ref)   # rewrite the field
```

No Extractor, no Auditor, no Arbiter. Just the normalizer tool patching the
canonical form into the envelope.

#### `re_link` — NO work in the repair node

```python
elif action == "re_link":
    outcome = "re_link_requested"   # just logs it
```

The actual re-linking happens via the graph edge `repair → linker` (Node 5
runs again). The repair node's job for `re_link` is just to acknowledge.

#### `drop_and_flag` — surgical envelope edit

```python
# Find the ref in section_outputs, set its value to None, queue the original
# in escalation_queue with kind="dropped_ungroundable" for SME.
```

No LLM. No team invocation.

### Then the graph follows `repair → linker`

After `RepairExecutor.apply()` returns, the graph edge fires:

1. **Linker** (Node 5) — re-assemble the (now-patched) `section_outputs`,
   re-run cross-section linking, dedup, supersession.
2. **Verifiers** (Node 6) — re-run all 8 verifiers + LinkBindingVerifier
   against the new envelope.
3. **Triage** (Node 7) — re-classify defects against the new scorecards.

If triage still has unfinished repair-eligible defects AND budget remains,
the loop continues: `triage → repair → linker → verifiers → triage`.

When triage returns `done` (no defects OR budget exhausted OR recur-guard
armed), the graph follows `triage → vmaw` and out of the loop.

### Repair log

Each successful repair appends one entry:
```
{cycle, action, target_ref, section, team, outcome, status, detail}
```

### Trace records emitted

```
phase: repair
  agent: RepairExecutor · re_extract_team    plain: "Our system re-ran the relevant team to take another pass."
  agent: RepairExecutor · reprofile_block    plain: "Our system re-classified a block of text and re-extracted from it."
  agent: RepairExecutor · renormalize_field  plain: "Our system normalised the field's value to its canonical form."
  agent: RepairExecutor · re_link            plain: "Our system asked the linker to reconsider an uncertain relationship."
  agent: RepairExecutor · drop_and_flag      plain: "Our system removed an ungrounded value and set it aside for review."
```

Important: when `re_extract_team` runs, the trace ALSO records the team's
internal Extractor/Auditor/Arbiter records (with `phase=extraction`, NOT
`phase=repair`). So a single repair cycle can produce ~5–10 trace records.

### Trace records emitted

```
phase: repair
  agent: RepairExecutor · re_extract_team    plain: "Our system re-ran the relevant team to take another pass."
  agent: RepairExecutor · reprofile_block    plain: "Our system re-classified a block of text and re-extracted from it."
  agent: RepairExecutor · renormalize_field  plain: "Our system normalised the field's value to its canonical form."
  agent: RepairExecutor · re_link            plain: "Our system asked the linker to reconsider an uncertain relationship."
  agent: RepairExecutor · drop_and_flag      plain: "Our system removed an ungrounded value and set it aside for review."
```

---

## Node 9 — `vmaw` (Verify, Mediate, Adjudicate, Witness)

**File**: `pipeline/vmaw.py:VMAWAgent.resolve()` (wrapped by `make_vmaw_node`)

**What it does**: deep resolution for escalations that couldn't be auto-fixed.
Three capabilities:

| Capability | What it does |
|---|---|
| **EC** (Expand Context) | Pull more surrounding text around the disputed value (broader block window). |
| **CITE** | Hunt for additional citations that support OR refute the disputed value. |
| **VA** (Value Adjudication) | When two candidate values exist, weigh evidence and propose the canonical one. |

Each escalation gets routed to one capability based on its defect kind. The
result is either:
- `auto_applied` — VMAW found clear evidence, patched the envelope.
- `proposed_for_sme` — VMAW has a suggestion but isn't confident, queue for
  human review with a value + citation.
- `dropped_ungroundable` — couldn't find any supporting evidence, mark absent.

### Trace records emitted

```
phase: vmaw
  agent: VMAW · EC     plain: "VMAW expanded the context window to look at more text…"
  agent: VMAW · CITE   plain: "VMAW hunted for a citation that supports (or refutes)…"
  agent: VMAW · VA     plain: "VMAW adjudicated between conflicting candidate values…"
```

---

## Node 10 — `decision_router`

**File**: `decision/decision_router.py:DecisionRouter.decide_v3()` (wrapped by
`make_decision_router_v3_node`)

**What it does**: the final verdict. Three possible outcomes:

| Verdict | Condition |
|---|---|
| `auto_accept` | All scorecards passed, no escalations, all team confidence ≥ threshold. |
| `partial_accept` | Some sections passed (commit those), others flagged for SME. |
| `sme_flag` | Document needs end-to-end human review. |

The router records, per section: `accepted_sections[]` (commit these),
`flagged_sections[]` (SME), `escalation_items[]` (the queue).

### Trace record emitted

```
phase: decision
agent: DecisionRouter
plain: "Our system made the final call on the whole document — verdict: <verdict>."
output_summary: "verdict=<verdict>"
reasoning: "<plain-language summary of the decision: how many sections accepted, what's flagged>"
```

---

## Node 11 — `persist`

**File**: `pipeline/graph_selfcorrecting.py:_make_selfcorrecting_persist_node`

Writes every artifact to disk (and to GCS in cloud runs):

```
local_runs/artifacts/<doc_id>/
   extraction_v2.json         # the unified envelope (written ALREADY by the linker, just refreshed)
   verification_v2.json       # scorecards + binding + links + attribution
   verification_v3.json       # raw v3 verifier output
   agent_trace.json           # the COMPLETE per-agent trace (this doc's main reference)
   repair_log.json            # one entry per repair_executor action
   vmaw_log.json              # one entry per VMAW resolution
   binding_items.json         # per-link binding verdicts (V1/V2/V3/V4)
   block_reads.json           # memoized AI re-reads from recall_floor
   escalation_queue.json      # the SME's review queue
   blocks.json, pages.json, parser_hypothesis.json, block_profiles.json  # (already from preprocess)
   word_geometry.json, source.pdf
```

And in the database (Firestore in cloud):
- `run` record — `doc_id`, `verdict`, `latency_ms`, `cost_usd`, `timestamps`.
- `extraction` record (only if verdict ∈ `auto_accept`, `partial_accept`) —
  the committed envelope.

---

## How the trace becomes the SME UI

After the graph finishes, the browser SPA (`ui/index.html` + `ui/app.js`) fetches
from `/api/docs` and `/artifacts/<doc>/<file>` served by `ui/app.py` (Flask). All
timeline assembly happens **client-side** in JS.

### Per-field trace assembly

The per-field timeline is assembled CLIENT-SIDE by `ui/app.js`'s
`renderFieldDetail` (around line 500) from the same `agent_trace.json` the
pipeline writes. When the reviewer clicks a row in the section-tab table, the
detail panel:

1. Reads the record's `provenance` array from `extraction_v2.json` — each entry
   carries the per-field `rationale` used as the "why (this field)" line.
2. Walks `agent_trace.json` and keeps records whose `section` matches OR whose
   `refs` touch the focused ref OR which are cross-section (Planner,
   DecisionRouter).
3. For the Extractor row, surfaces BOTH the per-field rationale AND the model's
   section-level reasoning (`agent_reasoning`) — labelled "why (this field)"
   and "section reasoning" respectively.
4. Sorts by `step` (the monotonic counter from `extend_trace`). The invoke
   order IS the render order.

`ui/agent-trace.js` renders the resulting timeline; `ui/pdf-viewer.js` handles
the page-highlight overlay when the reviewer opens the source PDF.

### Per-field "why this field" + "section reasoning"

`ui/app.js`'s `renderFieldDetail` renders up to two reasoning lines under each
timeline step:

```
↳ why (this field): <field_rationale — from provenance[i].rationale>
↳ section reasoning (what the model thought about the whole section):
  <reasoning block — from the model's <reasoning>…</reasoning> tags>
```

---

## Configs that drive every step — what they ARE, how to read them

A 60-second mental model: the code is generic; the configs are the contract. Every
schema-aware behaviour — Pydantic validation, prompt rendering, NER routing, dedup,
cross-section linking, scoring — reads ONE of these files to know what to do. You
add a field, you add a section, or you onboard a different document type by editing
configs and adjusting one or two ground-truth/scoring artifacts — almost never by
editing Python.

### `config/schemas/genomic_pathology_v4.json` — the extraction contract

A JSON Schema (Draft 2020-12). Top-level shape:

```jsonc
{
  "$schema": "...", "$id": "...", "title": "genomic_pathology_extraction",
  "schema_version": "v4",
  "type": "object",
  "additionalProperties": false,         // CRITICAL — unknown keys are rejected
  "required": [...],
  "properties": {
    "count_of_extracted_objects": {"type": "integer"},
    "report_metadata":                    {...},   // each of these is a SECTION
    "Genomic_Variant_umbrella":           {...},   // owned by one team
    "other_molecular_biomarker_umbrella": {...},
    "tested_biomarker_umbrella":          {...},
    "significant_findings":               {...},   // (disabled in v4)
    "clinical_information":               {...}    // (disabled in v4)
  }
}
```

Each SECTION is its own JSON-Schema sub-object that describes either:
- A flat object with named fields (e.g. `report_metadata` — patient details).
- An array umbrella: `count_of_X`, `llm_confidence_score`, and an array of record
  objects (e.g. `Genomic_Variant_umbrella.Genomic_Variants[]`).

Each FIELD inside a section carries metadata the extractor reads at runtime:

```jsonc
"gene_studied": {
  "type": ["string", "null"],            // null-allowed = optional
  "extraction_mode": "VERBATIM",          // VERBATIM | DERIVED | VERBATIM_OR_INFERRED — drives prompt rendering
  "description": "The gene tested for variants in this finding."
},
"coding_dna_change": {
  "type": ["string", "null"],
  "extraction_mode": "VERBATIM",
  "pattern": "^c\\.\\d+.*",               // optional — cosmetic check by jsonschema verifier
  "description": "HGVS coding-DNA change, e.g. c.1849G>T"
}
```

**Per-record provenance slot** (where it exists): inside each array's `items.properties`
you'll find a `provenance` field — that's what powers the per-field "why this field"
rationale line in the SME UI.

**How to read it programmatically**:
```python
from core.schema_loader import SchemaLoader
loader = SchemaLoader.from_path("config/schemas/genomic_pathology_v4.json")
loader.fields_for_section("Genomic_Variant_umbrella")   # list of {name,type,…}
loader.section_description("Genomic_Variant_umbrella")
loader.validate("Genomic_Variant_umbrella", payload)    # raises SchemaValidationError on shape mismatch
```

### `config/teams_v4.yaml` — the team registry

(v4 uses `teams_v4.yaml`; the older `teams.yaml` is v3 — both kept alongside
non-destructively. The runner picks based on `pipeline_version`.)

A team is one EXTRACTOR + AUDITOR + ARBITER bound to one schema section. Per team:

```yaml
metadata_team:
  schema_section: report_metadata                    # which section it owns
  prompt_template: config/prompts/metadata_team.j2   # team-specific overlay (base = system/extractor.j2)
  models:                                             # LLM choice per agent role
    extractor:        {name: gemini-2.5-pro,   temperature: 0.0}
    coverage_auditor: {name: gemini-2.5-pro,   temperature: 0.0}
    arbiter:          {name: gemini-2.5-flash, temperature: 0.0}
  tool_allowlist:                                     # tools the extractor may call
    - pdf_page_loader
    - npi_validator
    - date_parser
    - schema_validate
    - state_read
  auto_accept_confidence_threshold: 0.85              # decision router uses this
  coverage_gap_tolerance: 0.10                        # auditor's gap threshold
  enabled: false                                      # OPTIONAL — set to disable without deleting
```

### `config/section_layout.yaml` — the shape table

The Linker and scorer read this to be generic across sections (no hardcoded
section names in Python). For each array-bearing section it declares:

```yaml
Genomic_Variant_umbrella:
  record_array: Genomic_Variants        # which property holds the array
  gene_key_field: gene_studied           # which per-record field is the HGNC identity
  empty:                                 # placeholder when an active team produces nothing
    count_of_Genomic_Variants: 0
    llm_confidence_score: null
    Genomic_Variants: []

tested_biomarker_umbrella:
  record_array: tested_biomarkers
  gene_key_field: "*"                    # "*" = the array item IS the gene string
                                          # (no nested record object)
  empty: {count_of_tested_biomarkers: 0, page_numbers: [], llm_confidence_score: null, tested_biomarkers: []}

other_molecular_biomarker_umbrella:
  record_array: other_molecular_biomarkers
  gene_key_field: biomarker_name
  gene_key_requires: findings[*].variant_detail   # v3 only — optional filter
```

Plus a top-level `scalar_keys:` list for fields the model sometimes wraps in
`[…]` (the `_coerce_scalar_tics` rescues those — `page_number: [1] → 1`).

### `config/link_registry_v4.yaml` — cross-section link types

Every link type the Linker considers. Self-contained — adding a new type is one
YAML row, no Python:

```yaml
link_types:
  - type: variant_on_panel           # the type name written into trace + envelope
    tier: 1                          # tier 1 = always run; tier 2/3 = optional LLM-adjudicated
    active: true                     # quick disable without deletion
    from_section: Genomic_Variant_umbrella       # endpoint A
    to_section:   tested_biomarker_umbrella      # endpoint B
    description: >-
      A gene sequence variant (Genomic_Variant_umbrella) to the tested-panel
      entry for the same HGNC gene.
```

For each `(type, from, to)` entry the Linker walks both sections and tries to
match records via `gene_key_field` (resolved through HGNC canonicalization).
Self-typed pairs (from == to) automatically require `i ≠ j`.

### `config/dedup_policy.yaml` — who-owns-what rules

When the same gene/biomarker appears in two sections after extraction, this
table decides who wins and who is dropped:

```yaml
rules:
  - when_present_in: Genomic_Variant_umbrella     # OWNER — wins
    drop_from:       other_molecular_biomarker_umbrella   # loser — duplicate removed here
    match_on:        gene_key                     # how to detect "same entity"
    also_requires_filter_on_drop_from: findings[*].variant_detail   # only drop if this filter matches
```

`match_on: gene_key` uses each section's `gene_key_field` from
`section_layout.yaml`. So adding a dedup rule is one YAML row.

### `config/ner_mapping.yaml` — SciSpaCy → section routing

Drives `preprocess/medical_ner.py`. Six pieces:

```yaml
# (a) Which SciSpaCy/spaCy labels map to which umbrellas
label_to_umbrellas:
  GENE_OR_GENE_PRODUCT: [Genomic_Variant_umbrella, other_molecular_biomarker_umbrella, tested_biomarker_umbrella]
  PERSON:               [report_metadata]
  DATE:                 [report_metadata]
  # …labels NOT in this map are DROPPED

# (b) block roles where every entity is dropped (noise sections)
drop_roles:
  - fax_transport_noise
  - page_header
  - page_footer
  - electronic_signature

# (c) per-entity stoplist (case-insensitive false positives)
stoplist: [dna, rna, gene, mutation, ...]

# (d) per-field hint rules for metadata (regex over surface or context)
field_hint_rules: [...]
```

Pipeline per entity (in `medical_ner.py:_route`):
1. drop if block role ∈ `drop_roles`.
2. drop if surface ∈ `stoplist`.
3. lookup `label_to_umbrellas[label]` → list of allowed sections.
4. intersect with the block's `target_umbrella_hints` (from BlockProfiler) — narrow.
   Empty intersection → fall back to allowed (confidence 0.6); non-empty → 0.9.
5. group occurrences by `(surface, umbrella)`.
6. for `report_metadata`, assign `target_field_hint` via `field_hint_rules`.

### `config/prompts/system/*.j2` — the BASE prompts

Three system prompts shared by all teams:

| File | Used by | Renders into |
|---|---|---|
| `extractor.j2` | Extractor (Node 4a) | Field contract + recall mandate + ReAct loop + `<reasoning>` requirement + tool list + output format |
| `coverage_auditor.j2` | CoverageAuditor (Node 4b) | "Compare extractor output to parser hypothesis, flag misses/spurious" |
| `arbiter.j2` | Arbiter (Node 4c) | "Decide ACCEPT_EXTRACTOR / RE_EXTRACT / INVOKE_VMAW given the disagreement" |

The base extractor template is rendered via Jinja2 with this context:

```python
{team_name, pipeline_version,
 schema_section_name, schema_section_description,
 fields,                       # list of {name, type, required, description, extraction_mode, format, pattern}
 available_tools,              # list of {name, description, input_schema}
 parser_hypothesis_count}      # int — NER candidate count for this section
```

### `config/prompts/<team>_team.j2` — per-team OVERLAYS

Each team's prompt starts with `{% include 'system/extractor.j2' %}` (so it
inherits the base) then adds its domain rules:

```jinja
{% include 'system/extractor.j2' %}

---

# GenomicVariantTeam rules — `Genomic_Variant_umbrella`

## 1. What to include
- A separate record per distinct gene variant call.

## 2. Normalize
- Canonical HGNC for `gene_studied`. Call `hgnc_normalize` to resolve aliases.

## 3. HGVS contract
- `coding_dna_change` must include the `c.` prefix.
- `protein_change` may omit `p.` (we'll normalise downstream).

## 4. Concatenation + provenance
- Emit a `provenance` ARRAY on each variant — one entry per populated field.
- (the just-tightened rules about rationale being REQUIRED + good/bad examples)
```

The overlay is what specializes the generic Extractor for THIS team's domain.

### Where each config plugs into the running code

```
config/schemas/genomic_pathology_v4.json   ↘
                                             core/schema_loader.py  ──→  Pydantic validation
                                                                    └─→  extractor.j2 field contract
                                                                    └─→  scorer
                                                                    └─→  UI labels

config/teams_v4.yaml          ──→  pipeline/runner.py picks teams + models + tools per team
config/section_layout.yaml    ──→  agents/linker.py uses record_array, gene_key_field, empty
                              ──→  scoring/* uses record_array to find arrays
config/link_registry_v4.yaml  ──→  agents/linker.py walks pairs, runs validation
                              ──→  agents/link_binding_verifier.py picks which V-checks
config/dedup_policy.yaml      ──→  agents/linker.py:_apply_dedup_policy
config/ner_mapping.yaml       ──→  preprocess/medical_ner.py routing pipeline
config/prompts/system/*.j2    ──→  core/prompt_renderer.py base templates
config/prompts/<team>_team.j2 ──→  teams/section_team.py overlay (one per team)
```

---

## Change playbooks — "where do I touch?"

### Playbook A: Add a NEW FIELD to an existing schema section

Example: add `tumor_mutational_burden` (TMB) to `other_molecular_biomarker_umbrella`'s
`other_molecular_biomarkers[]` records.

**1. Schema** (`config/schemas/genomic_pathology_v4.json`):
- Inside `other_molecular_biomarker_umbrella.properties.other_molecular_biomarkers.items.properties`,
  add the field:
  ```jsonc
  "tumor_mutational_burden": {
    "type": ["string", "null"],
    "extraction_mode": "DERIVED",
    "description": "TMB score, e.g. '12 mut/Mb' — verbatim if printed; else inferred from raw count + panel size."
  }
  ```
- If it's required, add to the items `required` list.
- If it has a canonical form, add a `pattern` or `format`.

**2. Per-record provenance entry will need a rationale for this field too**
(automatic — once the team prompt is updated, the model emits one per populated field).

**3. Team prompt** (`config/prompts/molecular_biomarker_team_v4.j2`):
- The field contract is rendered automatically from the schema (no edit needed for
  the field bullet itself).
- BUT add domain rules if the field has special semantics:
  ```markdown
  ## 6. TMB

  - VERBATIM if the report prints a TMB score (e.g. "12 mut/Mb").
  - DERIVED is allowed when raw mutation count + panel size are both printed —
    cite both blocks in `provenance`.
  - Skip TMB unless it's clearly tumor-specific (not germline).
  ```

**4. Ground truth** (`ground_truth/demo_v4.json` etc.):
- For each fixture, add the expected `tumor_mutational_burden` value (`null` if absent).

**5. Scorer** (`scripts/score_against_ground_truth.py`):
- The scorer is field-driven — it picks up every property in the schema
  automatically. **Usually no edit needed.**
- If TMB needs special comparison (e.g. tolerate "12 mut/Mb" ≈ "12.0 mutations/megabase"),
  add a custom comparator in the team's section in the scorer.

**6. NER routing** (`config/ner_mapping.yaml`):
- Only edit IF a SciSpaCy label routes to TMB. TMB usually isn't a NER entity →
  no edit. If it IS, route via `label_to_umbrellas`.

**7. Dedup / Linker / `section_layout.yaml`**: NO edit. The new field is internal
to one section.

**8. Gates**: run `PYTHONPATH=. python scripts/gates/gate_v4_*.py` to confirm no
schema-shape lock was broken. Adjust GT fixtures inside any gate if it asserts the
old shape.

**Quick checklist**:
```
[x] schema add field
[x] team prompt domain rule (if non-trivial)
[x] ground truth fixtures
[ ] scorer (only if custom comparator)
[ ] NER mapping (only if SciSpaCy can detect it)
[ ] gates re-run
```

### Playbook B: Add a NEW SECTION to an existing schema (new team)

Example: add `pharmacogenomics_findings` (drug-gene interactions).

**1. Schema** (`config/schemas/genomic_pathology_v4.json`):
- Add a new property under top-level `properties`:
  ```jsonc
  "pharmacogenomics_findings": {
    "type": "object", "additionalProperties": false,
    "required": ["count_of_pgx_findings", "llm_confidence_score", "pgx_findings"],
    "properties": {
      "count_of_pgx_findings": {"type": "integer"},
      "llm_confidence_score": {"type": ["number", "null"]},
      "pgx_findings": {
        "type": "array",
        "items": {
          "type": "object", "additionalProperties": false,
          "required": ["gene", "drug", "evidence_block_ids"],
          "properties": {
            "gene":   {"type": "string"},
            "drug":   {"type": "string"},
            "phenotype": {"type": ["string", "null"]},
            "evidence_block_ids": {"type": "array", "items": {"type": "string"}},
            "provenance": {"type": ["array", "object", "null"]}
          }
        }
      }
    }
  }
  ```

**2. Team registry** (`config/teams_v4.yaml`):
- Add a team entry:
  ```yaml
  pgx_team:
    schema_section: pharmacogenomics_findings
    prompt_template: config/prompts/pgx_team.j2
    models:
      extractor:        {name: gemini-2.5-pro,   temperature: 0.0}
      coverage_auditor: {name: gemini-2.5-pro,   temperature: 0.0}
      arbiter:          {name: gemini-2.5-flash, temperature: 0.0}
    tool_allowlist:
      - state_read
      - schema_validate
      - hgnc_normalize
      - pdf_page_loader
    auto_accept_confidence_threshold: 0.85
    coverage_gap_tolerance: 0.10
  ```

**3. Team prompt** (new file `config/prompts/pgx_team.j2`):
- `{% include 'system/extractor.j2' %}` at the top.
- Then domain rules: drug-gene rules, evidence rules, provenance rules, ReAct
  loop closer.

**4. Section layout** (`config/section_layout.yaml`):
- Add a row:
  ```yaml
  pharmacogenomics_findings:
    record_array: pgx_findings
    gene_key_field: gene
    empty: {count_of_pgx_findings: 0, llm_confidence_score: null, pgx_findings: []}
  ```

**5. NER routing** (`config/ner_mapping.yaml`):
- Add the new section to the labels that should route to it:
  ```yaml
  label_to_umbrellas:
    GENE_OR_GENE_PRODUCT: [Genomic_Variant_umbrella, ..., pharmacogenomics_findings]
    CHEMICAL:             [pharmacogenomics_findings]   # drug names from SciSpaCy
  ```

**6. Cross-section links** (`config/link_registry_v4.yaml`) — OPTIONAL:
- If a PGx finding should link to its corresponding variant:
  ```yaml
  - type: pgx_to_variant
    tier: 1
    active: true
    from_section: pharmacogenomics_findings
    to_section:   Genomic_Variant_umbrella
    description: PGx drug-gene interaction tied to a detected variant of the same gene.
  ```

**7. Dedup policy** (`config/dedup_policy.yaml`) — OPTIONAL:
- Add a rule if the new section can conflict with an existing one.

**8. Ground truth** (`ground_truth/*.json`):
- For each fixture, add the new `pharmacogenomics_findings` object (with empty
  arrays where absent).

**9. Scorer** (`scripts/score_against_ground_truth.py`):
- The scorer walks `section_layout.yaml` — typically auto-discovers the new
  section's array. **Verify by running it; tweak if it needs section-specific
  comparators.**

**10. UI** (mostly automatic):
- The entity browser / production browser auto-discover sections from the
  envelope.
- `field_rationale_map` walks recursively — no edit needed.
- The Production label mapping (`config/production_mapping.yaml`) — only if you
  want the new section in the customer-facing output.

**11. Gates** — re-run:
```bash
PYTHONPATH=. python scripts/gates/gate_v4_m4_section_toggle.py
PYTHONPATH=. python scripts/gates/gate_v4_m5_link_registry.py
PYTHONPATH=. python scripts/gates/gate_v4_m13_trace_completeness.py
```

**Quick checklist**:
```
[x] schema add section + items.properties + required + provenance slot
[x] teams_v4.yaml add team entry
[x] config/prompts/<new>_team.j2 (include base, add domain rules)
[x] section_layout.yaml (record_array, gene_key_field, empty)
[x] ner_mapping.yaml (route SciSpaCy labels)
[ ] link_registry_v4.yaml (optional — only if section has cross-references)
[ ] dedup_policy.yaml (optional — only if section overlaps another)
[x] ground truth fixtures
[ ] scorer (only if custom comparators)
[ ] production_mapping.yaml (only if customer-facing)
[x] gates re-run
```

### Playbook C: Onboard a BRAND-NEW SCHEMA (different document type)

Example: a radiology-report extraction schema (totally different domain).

**1. New schema file** (`config/schemas/radiology_report_v1.json`):
- Authored by hand. Same structural conventions: top-level `properties`
  containing sections, each section an object with named fields OR a
  `count_of_X` + `X[]` array umbrella, each record with `evidence_block_ids` +
  `provenance`.

**2. New teams registry** (`config/teams_radiology_v1.yaml`):
- One team per top-level section.

**3. New prompt set** (`config/prompts/radiology_*.j2` + `config/prompts/system/*`
or a parallel system tree if needed):
- Each team's overlay starts with `{% include 'system/extractor.j2' %}`.
- The base system prompts are domain-agnostic — you usually DON'T have to fork
  them. The team overlays carry the radiology-specific rules.

**4. New section layout** (`config/section_layout_radiology.yaml`):
- One row per array-bearing section.

**5. New NER mapping** (`config/ner_mapping_radiology.yaml`):
- Different SciSpaCy models (en_ner_*) for radiology.
- New `label_to_umbrellas` table.

**6. New link registry** (`config/link_registry_radiology.yaml`):
- E.g. finding_on_image, finding_superseded_by, etc.

**7. New dedup policy** (`config/dedup_policy_radiology.yaml`):
- Empty if no cross-section overlap; else owner rules.

**8. Runner wiring** (`pipeline/runner.py`):
- Add a new `pipeline_version` entry in `_VERSIONS` mapping it to the schema +
  teams registry + section layout + link registry + dedup policy. About 10
  lines of dispatch — the only Python edit.

**9. Ground truth + scorer fixtures**:
- New folder `ground_truth/radiology/`.
- Run the scorer against it.

**10. UI**: views auto-discover. The Production browser needs a new mapping
file if customer-facing.

**11. Gates**: write a parallel set of `gate_radiology_*.py` gates that lock the
new contract — mirror the v4 gates structurally.

**Quick checklist**:
```
[x] schemas/radiology_report_v1.json
[x] teams_radiology_v1.yaml
[x] prompts/radiology_*.j2 (one per team)
[x] section_layout_radiology.yaml
[x] ner_mapping_radiology.yaml (different SciSpaCy models likely needed)
[x] link_registry_radiology.yaml
[x] dedup_policy_radiology.yaml
[x] pipeline/runner.py — dispatch entry for the new version
[x] ground_truth/radiology/* fixtures
[ ] scorer — usually auto-discovers, verify
[ ] production_mapping_radiology.yaml (if customer-facing)
[x] new gates locking the schema
```

---

## "What if I just want to…" — quick answers

| Task | Where to touch |
|---|---|
| Disable a team for one run | `teams_v4.yaml` → set `enabled: false` |
| Swap a team's LLM | `teams_v4.yaml` → change `models.extractor.name` |
| Give a team a new tool | `teams_v4.yaml` → add to `tool_allowlist` + ensure tool is registered in `core/tool_registry.py` (per `config/tools.yaml`) |
| Add a stoplist word to drop a SciSpaCy false positive | `ner_mapping.yaml` → `stoplist:` |
| Forbid SciSpaCy from routing CHEMICAL entities anywhere | `ner_mapping.yaml` → remove `CHEMICAL` from `label_to_umbrellas` (or add specific block roles to `drop_roles`) |
| Add a new auto-link between two sections | `link_registry_v4.yaml` → one new `link_types` entry |
| Disable cross-section dedup for a section pair | `dedup_policy.yaml` → comment out / delete the rule |
| Tighten the recall-floor sensitivity | `verification/recall_floor.py` + `config/recall_floor.yaml` — the yaml controls which `text_role` values count as "loud" vs "quiet" |
| Add a new repair primitive | `pipeline/repair.py` (code) + `pipeline/triage.py` to route to it (code) — not config-only yet |
| Change extraction-mode rules globally | `config/prompts/system/extractor.j2` (the VERBATIM/DERIVED contract section) |
| Make the model emit a different scratchpad format | `config/prompts/system/extractor.j2` (the `## Reasoning trace` section) |
| Strengthen per-field rationale rules | the relevant team's `config/prompts/<team>_team.j2` (the provenance contract section) |

---

## Agent trace — the SME's source of truth

Every node writes to one channel: `state["agent_trace"]`. The recorder
(`core/trace_recorder.py:record()` + `extend_trace()`) assigns each record:

```
{step:    <monotonic int>,
 phase:   preprocess | planning | extraction | linking | supersession |
          dedup | verification | triage | repair | vmaw | decision,
 agent:   "Extractor" | "Linker · variant_on_panel" | "Triage · repair · …" | …,
 plain:   "<one-sentence SME-facing summary, written at record time>",
 section: "<v4 section name>" | None (cross-section agents),
 refs:    ["<ref>", …] (records the agent acted on — for per-field filtering),
 input_summary, output_summary, verdict, reasoning, field_reasons,
 confidence, latency_ms, tool_calls, team}
```

By the end of a run, `agent_trace.json` is the **complete decision log** — one
file, every agent, in order. The UI's `renderFieldDetail` (in `ui/app.js`)
filters this list by section/ref client-side to produce a per-field timeline.

---

## Putting it all together — concrete example

Given `make run-local PDF=./data/actual_docs/demo.pdf PHASE=4`, you'll see in
`agent_trace.json` (in this order, ~76 records for the demo):

```
step 0  preprocess     DocAIParser            "parsed 2 page(s), 70 block(s)"
step 1  preprocess     FaxHeaderFilter        "flagged 2 fax-noise block(s)"
step 2  preprocess     BlockProfiler          "70 profiles; roles + section hints"
step 3  preprocess     MedicalNER             "40 candidate(s) by section"
step 4  planning       Planner                "active=[4 teams] skipped=[]"
step 5  extraction     Extractor (metadata)   "produced 26 populated field(s)"
step 6  extraction       Extractor · thought #1
step 7  extraction       Extractor · tool · state_read
…
step 23 extraction     CoverageAuditor (metadata)
step 24 extraction     Extractor (genomic_variant) ← 891-char <reasoning> block
…
step 30 extraction     Extractor (other_molecular_biomarker)
…
step 35 extraction     Extractor (tested_biomarker)
step 36 linking        Linker                 "assembled 4 section(s); 1 link emitted"
step 37 linking        Linker · variant_on_panel "JAK2 (variant) ↔ JAK2 (panel)"
step 38 verification   schema_validator       "v4 envelope structurally valid"
step 39 verification   coverage_audit         "no gaps"
step 40 verification   link_consistency       "1 link(s), 0 issue(s)"
…
step 47 verification   LinkBindingVerifier    "refuted=0 uncertain=0"
step 48 verification   LinkBindingVerifier · V4
step 49 triage         Triage                 "route=done; 0 repairs, 0 escalations"
step 50 decision       DecisionRouter         "verdict=auto_accept"
```

Plus the artifacts on disk that the UI consumes:

```
extraction_v2.json    ← the envelope
verification_v2.json  ← scorecards
agent_trace.json      ← the timeline above
```

The browser SPA (`ui/index.html` + `ui/app.js`) then turns this into the per-field
experience: pick a doc → pick a section tab → click a row → the detail panel shows
the timeline (filtered client-side to records that touched THIS field) plus the
per-field rationale + section reasoning side-by-side on the Extractor row.

---

## Quick reference — file → role

| File | Role |
|---|---|
| `pipeline/runner.py` | Entry point. Loads configs, builds the graph, invokes. |
| `pipeline/graph_selfcorrecting.py` | Assembles the LangGraph state machine (11 nodes). |
| `pipeline/graph_linear.py` | Holds the team/linker/verifier nodes (reused by v3/v4). |
| `preprocess/preprocess_node.py` | The 4-sub-agent preprocess node. |
| `preprocess/docai_parser.py` | DocAI bbox extraction. |
| `preprocess/fax_header_filter.py` | Fax noise filter. |
| `preprocess/block_profiler.py` | Gemini block role/section classifier. |
| `preprocess/medical_ner.py` | SciSpaCy + deterministic post-processor. |
| `agents/planner.py` | Deterministic team activation. |
| `teams/section_team.py` | The 4-agent ReAct loop (Extractor → Auditor → Arbiter → re-extract). |
| `agents/extractor.py` | The main ReAct extractor. |
| `agents/coverage_auditor.py` | The second-pass recall check. |
| `agents/arbiter.py` | The disagreement mediator. |
| `agents/linker.py` | Assembly + dedup + supersession + cross-section linking. |
| `agents/link_binding_verifier.py` | V1/V2/V3/V4 LLM binding checks. |
| `pipeline/triage.py` | Defect classification + routing. |
| `pipeline/repair.py` | The 5 repair primitives. |
| `pipeline/vmaw.py` | EC / CITE / VA deep resolution. |
| `decision/decision_router.py` | The auto_accept / partial / sme_flag verdict. |
| `core/schema_loader.py` | Schema → Pydantic at runtime. |
| `core/prompt_renderer.py` | Jinja2 env for all team/system prompts. |
| `core/trace_recorder.py` | `record()` + `extend_trace()` — the agent_trace recorder. |
| `core/persistence.py` | Artifact + Firestore writes. |
| `verification/*` | The 8 deterministic verifiers. |
| `ui/app.py` | Flask server — serves `index.html`, `/api/docs` (scans `local_runs/artifacts/`), and `/artifacts/<doc>/<file>`. |
| `ui/index.html` | Single HTML page — dashboard queue + per-doc review with four section tabs. |
| `ui/app.js` | Main JS module (`window.app`). `renderFieldDetail` assembles the per-field timeline client-side from `agent_trace.json` + `extraction_v2.json`. |
| `ui/agent-trace.js` | Renders the per-field agent timeline. |
| `ui/pdf-viewer.js` | PDF viewer with page-highlight support. |
| `ui/styles.css` | All styling. |
