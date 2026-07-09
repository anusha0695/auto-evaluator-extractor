# Agent Architecture — what is an agent here, and why

This document defines, rigorously, what qualifies a component as an **agent** in
this pipeline, then classifies every LLM-touching component against that
definition. Three components are true agents — **Extractor, VMAW, and Linker** —
and this doc states explicitly *how each one qualifies*. Everything else that
uses an LLM is a **single structured call**, and the doc states *why a single
call is the correct design there*.

> Scope note: this document describes the **agentic** target architecture in
> which VMAW and Linker let the LLM direct their own control flow (not a fixed
> routing table or a one-shot adjudicator). Where that differs from a simpler
> implementation, the doc says so.

---

## 1. The definition — 7 requirements

A component is an **agent** when the LLM **dynamically directs its own control
flow**: it decides, at runtime, what action to take next, observes the result,
and decides again — pursuing a goal whose sub-steps cannot be fully specified in
advance.

The discriminating test (R5) is ownership of control flow: does the **LLM**
choose the next step, or does **predefined code**? If code owns the sequence,
it's a *workflow*, not an agent — no matter how much reasoning happens inside
each step.

| # | Requirement | Test |
|---|---|---|
| **R1** | LLM-driven decisions | A model — not rules — makes the core decision. |
| **R2** | Action capability | It can act on an environment: call tools, fetch data, mutate state. |
| **R3** | Observation feedback | It reads the *result* of each action and feeds it back in. |
| **R4** | Iteration | It operates over multiple turns, not one-shot. |
| **R5** | **Dynamic control flow** ← discriminator | The **LLM** decides the next action at runtime; the path is NOT fixed in code. |
| **R6** | Goal-directed autonomy | It pursues a goal by self-determining sub-steps. |
| **R7** | Working memory | It carries state across steps. |

**Classification:**
- **Agent** = R1–R7, *especially R5*.
- **Single LLM call** = R1 only (one prompt → one structured output).
- **Rules** = no R1 (deterministic).

---

## 2. The three agents

### 2.1 Extractor — section extraction agent

**Goal**: produce a complete, valid, grounded extraction of one schema section
from a document it cannot fully see at prompt time.

**Tools** (bound via `llm.bind_tools` in `agents/extractor.py`): `pdf_page_loader`,
`pdf_text_search`, `docai_layout_lookup`, `state_read`, `schema_validate`,
`hgnc_normalize`, `hgvs_validate`, `biomarker_normalize`, `method_normalize`,
`date_parser`, `npi_validator`.

**How it qualifies as an agent:**

| Req | Satisfied? | How |
|---|---|---|
| R1 | ✅ | Gemini decides what to extract and how. |
| R2 | ✅ | Calls 11 bound tools. |
| R3 | ✅ | Reads each tool's return and reacts (e.g. uses the canonical gene the resolver returned). |
| R4 | ✅ | ReAct loop, up to 12 iterations. |
| R5 | ✅ | **The model chooses the next tool from what it just found** — saw a gene → `hgnc_normalize`; saw an HGVS change → `hgvs_validate`; needs an unseen page → `pdf_page_loader`. |
| R6 | ✅ | Goal = valid section extraction; it self-sequences the sub-steps. |
| R7 | ✅ | `messages[]` accumulates as working memory. |

**Why it MUST be an agent (a single call cannot do this):** the document is too
large to fit in one prompt (the 100+ block case), and the model cannot know in
advance which genes it will need to canonicalize or which pages it will need to
read. The right sequence of lookups is data-dependent — discoverable only at
runtime — so the LLM must own the control flow.

**Proof it acts as an agent in real runs**: the demo run's trace shows the
Extractor calling `pdf_page_loader` 2×, `pdf_text_search` 3×, `hgvs_validate` 2×,
`hgnc_normalize` 1×, `date_parser` 4× — each call chosen dynamically mid-reasoning.

---

### 2.2 VMAW — escalation-resolution agent

**Goal**: resolve an escalated defect (a flagged field/record the deterministic
pipeline couldn't settle) by gathering more evidence from the source and deciding
whether to auto-apply a fix, propose one to the SME, or drop the value.

**Tools / capabilities** (the agent's action set):
- **EC (Expand Context)** — pull more source text around the disputed value.
- **CITE** — hunt the source for a citation that supports or refutes the value.
- **VA (Value Adjudication)** — weigh competing candidate values and pick one.
- **apply** — mutate the envelope with a grounded resolution.
- **drop** — remove an ungroundable record (kept for SME audit).

**How it qualifies as an agent:**

| Req | Satisfied? | How |
|---|---|---|
| R1 | ✅ | Gemini decides how to resolve each escalation. |
| R2 | ✅ | EC/CITE read source text; `apply`/`drop` mutate the envelope. |
| R3 | ✅ | Reads each capability's result (grounded? contested? value found?) and reacts. |
| R4 | ✅ | Loops over capabilities until resolved or exhausted. |
| R5 | ✅ | **The model chooses the next capability from what the last one returned** — "CITE found no citation → expand context with EC → now adjudicate with VA." The capability sequence is discovered at runtime, not fixed. |
| R6 | ✅ | Goal = a defensible resolution; it self-determines which capabilities to chain. |
| R7 | ✅ | Carries what it has tried + partial findings across capability calls. |

**Why it qualifies (the R5 promotion):** in the agentic design, VMAW does NOT
read a static routing table to decide EC-vs-CITE-vs-VA. It reasons: *"this defect
is a malformed value with a likely source mention → try CITE; CITE came back
ungrounded → the value may be split across lines, expand context with EC; now I
have two candidate spellings → adjudicate with VA."* Each choice depends on the
prior capability's output. That data-dependent, runtime-decided sequencing is
exactly R5 — the LLM owns the resolution path.

**Why it MUST be an agent (a single call cannot do this):** resolving a defect
requires *iterative evidence gathering* — you don't know whether a citation
exists until you search, and you don't know whether to adjudicate until you've
seen the candidates. The number and order of evidence-gathering steps varies per
defect and cannot be predefined. A single call would have to either resolve
blind (no fresh evidence) or be pre-fed every possible source span (impossible).

**Autonomy boundary (safety):** even as an agent, VMAW's *terminal* action is
gated by policy, not pure autonomy — a contested value adjudication never
auto-applies; it always proposes to the SME. The agent owns *how it investigates*;
the *commit decision* on contested values stays human. This is deliberate: agentic
investigation + conservative commit.

---

### 2.3 Linker — cross-section linking agent

**Goal**: assemble the per-team section outputs into one envelope and discover
the correct set of cross-section relationships (a variant on a panel, a result
for a tested gene, an addendum superseding a finding), then reconcile duplicates
— both **cross-section** (owner-wins per `config/dedup_policy.yaml`) and
**intra-section** (`_apply_intra_section_dedup`: collapse or supersede records
at the same identity, e.g. two KRAS p.G12D records restated on different pages).

**Tools / capabilities** (the agent's action set):
- **get_records(section)** — fetch the records of a section to compare.
- **gene_key_match(a, b)** — HGNC-canonical gene comparison between two records.
- **propose_link(type, from, to)** — propose a typed link for validation.
- **validate_link(link)** — deterministic check (type ∈ registry, endpoints
  resolve, evidence cited, confidence ≥ floor).
- **dedup(rule)** — drop a cross-section duplicate per the ownership policy.

**How it qualifies as an agent:**

| Req | Satisfied? | How |
|---|---|---|
| R1 | ✅ | Gemini decides which records to compare and which links to form. |
| R2 | ✅ | Fetches records, proposes links, validates, dedups. |
| R3 | ✅ | Reads match results + validation outcomes and reacts (drop a refuted link, keep a confirmed one). |
| R4 | ✅ | Iterates across section pairs and candidate links. |
| R5 | ✅ | **The model chooses which section pair to examine next and which link to attempt, based on what it found** — "JAK2 in variants matched JAK2 in the panel → propose variant_on_panel → validated → now check the biomarker section for the same gene." |
| R6 | ✅ | Goal = a complete, consistent link set; it self-determines the comparison order. |
| R7 | ✅ | Accumulates the committed link set + the records already reconciled. |

**Why it qualifies (the R5 promotion):** in the agentic design, the Linker does
not run a fixed `gene_key_links() → one adjudicator call` pipeline. It *explores*:
decides which sections to compare, requests their records, runs gene-key matches,
proposes links, reads the validation verdict, and decides what to examine next —
including following a lead ("this addendum block references an earlier finding —
let me check whether it supersedes it"). The order and selection of comparisons
is data-dependent and decided at runtime.

**Why it MUST be an agent (a single call cannot do this):** the link space is
combinatorial and context-dependent. Which records to compare, and whether a
contextual reference ("the above variant") points at a specific finding, can't
be enumerated in one prompt — the agent has to look, match, validate, and follow
leads. A single call would either miss contextual links (no exploration) or be
fed every possible pairwise comparison (combinatorial blow-up).

**Determinism boundary (safety):** the Linker-agent's *proposals* are LLM-driven,
but every committed link still passes the deterministic `validate_link` gate
(type ∈ registry, endpoints resolve, evidence cited). The agent owns *discovery*;
the *commit* is gated by rules. Agentic exploration + deterministic validation.

---

## 3. The single-LLM-call components — and why they are NOT agents

These components use an LLM but make **one structured call**: one prompt in, one
typed object out. They are not agents, and that is the correct design — because
all the information they need is already in the prompt, and there is no
data-dependent next step to decide. Adding an agent loop would add latency, cost,
and nondeterminism for zero information gain.

### 3.1 CoverageAuditor — single call

- **What it does**: compares the Extractor's output against the NER candidates +
  page text and flags what's missing or spurious.
- **Why a single call, not an agent**: everything it needs (the output, the
  candidates, the text) is in the prompt. There is no "next action to decide" —
  it renders one verdict. R5 is moot: there is no control flow to own. It has no
  tools bound (`no tools` in its own source).
- **Why a separate call and not folded into the Extractor**: *independence*. The
  Auditor gets a fresh prompt so it isn't anchored on the Extractor's answer — a
  second pair of eyes. A model grading its own output would rationalize it.

### 3.2 Arbiter — single call

- **What it does**: given an Extractor↔Auditor disagreement, decides
  ACCEPT_EXTRACTOR / RE_EXTRACT / INVOKE_VMAW.
- **Why a single call, not an agent**: it *emits a routing decision* but does not
  *execute* it — the team code acts on the returned label. Emitting a decision is
  not owning control flow. It needs no evidence-gathering: the dispute + evidence
  are in the prompt. One bounded judgment → one call.

### 3.3 BlockProfiler — single call (batched)

- **What it does**: labels each layout block's role + section hint.
- **Why a single call, not an agent**: classification over the blocks already in
  the prompt. No lookups, no next step. Batched 300 blocks per call for cost.

### 3.4 Adjudicators (relationship-confirm, link-confirm, supersession, merge, contextual) — single calls

- **What they do**: bounded semantic yes/no judgments ("do these describe the
  same gene?", "does this addendum supersede that finding?").
- **Why single calls, not agents**: each is one prompt → one verdict. In the
  agentic Linker design (§2.3) these become *capabilities the Linker-agent
  invokes* — but each individual adjudication is still a single call. The agent
  is the orchestrator; the adjudicators are its one-shot tools.

### 3.5 LinkBindingVerifier V2/V4 — single calls

- **What they do**: V2 (does this result belong to THIS test?) and V4 (does this
  cross-reference hold?) — reading-comprehension checks per link.
- **Why single calls**: each check is a bounded judgment over the link + its
  cited evidence, both in the prompt. (V1/V3 are deterministic, no LLM.)

---

## 4. The rules components — and why no LLM at all

These are deterministic. They use no LLM because their logic is fully
specifiable and you want 100% reproducibility — an LLM would trade a
guaranteed-correct answer for a probabilistic one.

| Component | Why rules, not LLM |
|---|---|
| **Planner** | Team activation = config flags + block hints. Exact answer. |
| **8 Verifiers** | Each is a specifiable yes/no (schema match, refs resolve, evidence cited, HGVS valid). |
| **RepairExecutor** | Dispatches repair_requests to primitives. No judgment. |
| **DecisionRouter** | Aggregation + threshold checks → verdict. Arithmetic. |
| **FaxHeaderFilter** | Regex patterns for transport noise. |
| **MedicalNER** | SciSpaCy span detection + deterministic label→section mapping. |
| **DocAIParser** | OCR/layout via Document AI (vision task, not reasoning). |

---

## 5. The principle, stated once

Every component uses **the cheapest mechanism that is correct**:

```
Rules        → the answer is fully specifiable; you want reproducibility
               (Planner, Verifiers, DecisionRouter, RepairExecutor, FaxFilter)

Single call  → a bounded judgment over information already in the prompt;
               no data-dependent next step to decide
               (CoverageAuditor, Arbiter, BlockProfiler, Adjudicators, V2/V4)

Agent        → the task is open-ended: the LLM must gather evidence it does
               not have at prompt time, and each finding changes the next step
               (Extractor, VMAW, Linker)
```

You add agent-ness (R5 — the LLM owning its control flow) **only when the right
sequence of steps cannot be predefined in code.** That is true in exactly three
places:

- **Extractor** — can't predefine which genes to normalize or pages to load.
- **VMAW** — can't predefine how many evidence-gathering steps a defect needs.
- **Linker** — can't predefine which cross-section comparisons to make or which
  contextual leads to follow.

Everywhere else, the steps *can* be specified, and specifying them buys
determinism, auditability, lower cost, and regression tests — which, for clinical
data extraction, are worth more than dynamic autonomy. Agents cost control; you
pay that cost precisely where the task is genuinely open-ended, and nowhere else.

---

## 6. Safety pattern shared by all three agents

All three agents pair **agentic discovery** with a **non-agentic commit gate** —
the LLM owns *how it investigates*, but a deterministic or policy check owns *what
gets committed*:

| Agent | LLM owns | Commit gated by |
|---|---|---|
| Extractor | which tools to call, what to extract | Pydantic schema validation before the value is accepted |
| VMAW | which capabilities to chain | policy — contested adjudications never auto-apply, they propose to SME |
| Linker | which records to compare, which links to propose | deterministic `validate_link` (type ∈ registry, endpoints resolve, evidence cited) |

This is the core design stance: **agentic exploration, deterministic
commitment.** The agent is free to investigate dynamically; nothing reaches the
final envelope without passing a check that is NOT the agent's own judgment.
