"""
PipelineState — the LangGraph state object.

Every node in `pipeline/graph_vN.py` reads and writes this TypedDict. Keep it
flat-ish so LangGraph's checkpointing (Firestore in our setup) serializes
cleanly; nested Pydantic models are converted via `model_dump()` on write.

Conventions:

- Fields set by upstream nodes are read by downstream nodes; nothing in here
  is shared mutable state (LangGraph re-runs nodes on state mutation).
- Optional fields default to `None` until populated by their owning node.
- `pipeline_version` is set at the runner entry point and threaded through
  for trace / persistence tagging.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict


# ---------------------------------------------------------------------------
# Sub-shapes
# ---------------------------------------------------------------------------


class BlockInfo(TypedDict, total=False):
    """One DocAI layout block.

    M2.5: blocks that came from a DocAI table cell carry table-structure
    coordinates so the Binder can bind name↔method↔result deterministically by
    row, and so a spanning header/cell expands across the rows/cols it covers.
    These are absent (None) for non-table blocks.
    """

    block_id: str
    page_number: int
    bbox: list[float]                 # [x0, y0, x1, y1] in DocAI's normalized space
    section_path: str                 # e.g. "page_1/header/right_column"
    text: str
    # --- table structure (M2.5; only on table-cell blocks) ----------------
    table_id: str                     # stable id for the owning table (stitched across pages)
    row: int                          # 0-based row index within the table (header rows first)
    col: int                          # 0-based column index
    row_span: int                     # DocAI cell row_span (default 1)
    col_span: int                     # DocAI cell col_span (default 1)
    is_table_header: bool             # True if the cell is in a header row
    table_continued: bool             # True if this row came from a stitched continuation page


UmbrellaHint = Literal[
    "report_metadata",
    "other_molecular_biomarker_umbrella",
    "tested_biomarker_umbrella",
    # Phase 2 (schema v3):
    "significant_findings",
    "clinical_information",
    "none",
]


class BlockProfile(TypedDict, total=False):
    """One Block Profiler classification for a single block.

    R2: `target_umbrella_hints` is a LIST — a single block can legitimately
    feed more than one umbrella (e.g. a results_table holds both gene-level
    variant rows AND panel membership, so it hints both Genomic_Variant and
    tested_biomarker). `["none"]` means no team needs this block.
    """

    block_id: str
    page_number: int
    text_role: str                    # closed vocab in config/prompts/preprocess/block_profiler.j2
    target_umbrella_hints: list[UmbrellaHint]
    confidence: float
    rationale: str


class BlockSpan(TypedDict, total=False):
    """Where a block's text sits inside the concatenated page text.

    Recorded at concat time in docai_parser so the Medical NER stage can map
    a SciSpaCy entity's page-relative char offset back to its source block
    EXACTLY (no fuzzy substring matching). `start`/`end` are offsets into the
    owning PageText.text.
    """

    block_id: str
    start: int
    end: int


class PageText(TypedDict, total=False):
    """Per-page cleaned text emitted by DocAI + fax_header_filter."""

    page_number: int
    text: str
    block_ids_on_page: list[str]
    block_spans: list[BlockSpan]      # 2C: block_id → [start, end] in `text`


class WordBox(TypedDict, total=False):
    """One OCR token's geometry (P3-M8a). Captured from `document.pages[].tokens`
    when the response has an OCR layer; powers the SME UI's pixel-tight entity
    highlight (via `preprocess/word_geometry.word_boxes_for_entity`). Empty when
    the doc is pure Layout-Parser output → UI falls back to the block box."""

    page: int
    text: str
    bbox: list[float]                 # [x0, y0, x1, y1] normalized
    char_start: int
    char_end: int


class DocProfile(TypedDict, total=False):
    """Aggregated preprocessing output for the doc."""

    total_pages: int
    pages: list[PageText]
    blocks: list[BlockInfo]
    block_profiles: list[BlockProfile]
    word_geometry: list[WordBox]      # M8a: OCR token boxes (may be empty)
    raw_docai_gcs_uri: str            # where the raw DocAI response is cached
    fax_header_blocks_removed: int    # count of blocks dropped by fax_header_filter


class Occurrence(TypedDict, total=False):
    """One place an entity appeared. 1A: candidates carry the full list of
    occurrences so a downstream specialist team can see every mention site
    (with its block + role), not just a flattened page list.
    """

    block_id: str
    page: int
    char_start: int
    char_end: int
    text_role: str                    # role of the containing block
    evidence_excerpt: str             # ~120 chars around the hit


class ParserHypothesisCandidate(TypedDict, total=False):
    """One candidate emitted by the Medical NER adapter.

    1A: `occurrences` is the source-of-truth provenance list. `pages` and
    `evidence_excerpt` are DERIVED from it (kept for back-compat with the
    CoverageAuditor, which reads them today).
    """

    text: str
    target_umbrella: Literal[
        "report_metadata",
        "other_molecular_biomarker_umbrella",
        "tested_biomarker_umbrella",
        "significant_findings",
        "clinical_information",
    ]
    target_field_hint: str | None     # schema field name, or None for non-metadata umbrellas
    occurrences: list[Occurrence]     # 1A: every mention site, grouped under this (text, umbrella)
    pages: list[int]                  # DERIVED: sorted(unique o.page for o in occurrences)
    evidence_excerpt: str             # DERIVED: occurrences[0].evidence_excerpt
    source_models: list[str]
    confidence: float
    rationale: str


class ParserHypothesis(TypedDict, total=False):
    """Output of preprocess/medical_ner.py."""

    doc_id: str
    candidates: list[ParserHypothesisCandidate]
    counts_by_umbrella: dict[str, int]
    dropped_count: int
    dropped_examples: list[dict[str, str]]


class VerifierScorecard(TypedDict, total=False):
    """One verifier's verdict on the team output."""

    verifier_name: Literal[
        "schema_validator",
        "coverage_audit",
        "link_consistency",
        "evidence_confidence",
    ]
    passed: bool
    field_errors: list[dict[str, Any]]
    notes: str


# ---------------------------------------------------------------------------
# PipelineState — the top-level TypedDict
# ---------------------------------------------------------------------------


class PipelineState(TypedDict, total=False):
    """LangGraph state object passed between nodes.

    `total=False` means every key is optional at construction; nodes set the
    keys they own. The runner seeds `doc_id`, `pipeline_version`, and one of
    `gcs_uri` / `raw_pdf_bytes`.
    """

    # Identity + provenance ----------------------------------------------------
    doc_id: str
    pipeline_version: Literal["v1", "v2", "v3", "v4"]
    gcs_uri: str | None                       # gs://patient_clinical_trial/patient_profiles/<doc>.pdf
    raw_pdf_bytes: bytes | None               # only set when running on a local PDF (no GCS URI)

    # Preprocessing outputs ----------------------------------------------------
    doc_profile: DocProfile | None
    parser_hypothesis: ParserHypothesis | None
    preprocessing_errors: list[dict[str, Any]]

    # Team outputs -------------------------------------------------------------
    # Keyed by team name (e.g. "metadata_team"); value is the team's emitted
    # JSON conforming to the matching schema section.
    team_outputs: dict[str, dict[str, Any]]

    # Linking output (Phase 2+) ------------------------------------------------
    # The fully-assembled schema-v2 envelope. In Phase 1 the Linking step is
    # trivial — wrap the MetadataTeam output in the full envelope with the
    # other 3 umbrellas as empty placeholders.
    extraction: dict[str, Any] | None

    # Phase 2 (graph_linear) inter-node channels --------------------------------
    # CRITICAL: LangGraph's StateGraph(PipelineState) only propagates keys that
    # are DECLARED here as channels — undeclared keys returned by a node are
    # dropped before the next node runs. graph_v1 routes its data through the
    # declared `team_outputs`; graph_linear passes the teams' section outputs to the
    # Linker (and team/binding verdicts to the decision router + UI) via these,
    # so they MUST be declared or the Linker sees nothing and emits an empty
    # envelope.
    section_outputs: dict[str, Any]          # schema_section → that team's payload
    team_results: dict[str, Any]             # team_key → {verdict, confidence, needs_review_count}
    active_team_keys: list[str]              # Planner output
    planner_rationale: dict[str, str]        # team_key → why active/skipped
    links: list[dict[str, Any]]              # Linker cross-section links
    link_needs_review: list[dict[str, Any]]  # Linker escalation refs
    binding_verifier: dict[str, Any]         # {refuted, uncertain} summary
    binding_items: list[dict[str, Any]]      # per-verdict {ref, check, verdict, evidence} (trace "why")

    # Verification + decision --------------------------------------------------
    verifier_scorecards: list[VerifierScorecard]
    # P3-M4 added "partial_accept" (graph_selfcorrecting router commits clean sections,
    # escalates the rest). "fixable" is the graph_linear dead branch kept for compat.
    verdict: Literal["auto_accept", "partial_accept", "fixable", "sme_flag"] | None
    verdict_reason: str | None
    router_decision: dict[str, Any]          # P3-M4 RouterDecisionV3.to_dict() (accepted/flagged/escalation_items)

    # Phase 3 (graph_selfcorrecting) repair-loop channels ---------------------------------
    # CRITICAL: LangGraph drops undeclared keys between nodes (the empty-extraction
    # bug). The triage→repair→linker→verifiers→triage cycle threads these:
    repair_budget_used: int                  # graph-level ping-backs consumed this doc
    repair_requests: list[dict[str, Any]]    # triage's selected catalog actions for THIS cycle
    defect_signatures_seen: list[str]        # "(team, target_ref, defect_type)" — recur-guard
    block_reads: dict[str, Any]              # memoized recall-floor re-read determinations (present/absent)
    repair_log: list[dict[str, Any]]         # per-cycle ledger (persisted as repair_log.json)
    escalation_queue: list[dict[str, Any]]   # record-level items for the SME queue (M8 UI)
    relink_hints: list[dict[str, Any]]       # gap #3: refuted link refs → re-link adjudicator "avoid/re-evaluate"

    # P3-M7 VMAW deep-resolution outputs ---------------------------------------
    vmaw_log: list[dict[str, Any]]           # per-item resolution ledger (persisted as vmaw_log.json)
    vmaw_resolutions: list[dict[str, Any]]   # VMAWResolution dicts (auto_applied / proposed_for_sme / unresolved)

    # P3-M8b per-agent trace (PHI-safe summaries; persisted local-only) --------
    agent_trace: list[dict[str, Any]]        # AgentTraceRecord dicts across all teams (the "how it got extracted" timeline)

    # Persistence + audit ------------------------------------------------------
    artifacts_gcs_prefix: str | None          # gs://.../extraction_outputs/phase1/<doc_id>/
    bigquery_extraction_row_id: str | None
    bigquery_run_row_id: str | None

    # Observability ------------------------------------------------------------
    otel_trace_id: str | None
    cost_usd: float                           # accumulated as agents run
    latency_ms: int                           # total wall-clock for the run
