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
    """One DocAI layout block."""

    block_id: str
    page_number: int
    bbox: list[float]                 # [x0, y0, x1, y1] in DocAI's normalized space
    section_path: str                 # e.g. "page_1/header/right_column"
    text: str


class BlockProfile(TypedDict, total=False):
    """One Block Profiler classification for a single block."""

    block_id: str
    page_number: int
    text_role: str                    # closed vocab in config/prompts/preprocess/block_profiler.j2
    target_umbrella_hint: Literal[
        "report_metadata",
        "Genomic_Variant_umbrella",
        "other_molecular_biomarker_umbrella",
        "tested_biomarker_umbrella",
        "none",
    ]
    confidence: float
    rationale: str


class PageText(TypedDict, total=False):
    """Per-page cleaned text emitted by DocAI + fax_header_filter."""

    page_number: int
    text: str
    block_ids_on_page: list[str]


class DocProfile(TypedDict, total=False):
    """Aggregated preprocessing output for the doc."""

    total_pages: int
    pages: list[PageText]
    blocks: list[BlockInfo]
    block_profiles: list[BlockProfile]
    raw_docai_gcs_uri: str            # where the raw DocAI response is cached
    fax_header_blocks_removed: int    # count of blocks dropped by fax_header_filter


class ParserHypothesisCandidate(TypedDict, total=False):
    """One candidate emitted by the Medical NER adapter."""

    text: str
    target_umbrella: Literal[
        "report_metadata",
        "Genomic_Variant_umbrella",
        "other_molecular_biomarker_umbrella",
        "tested_biomarker_umbrella",
    ]
    target_field_hint: str | None     # schema field name, or None for non-metadata umbrellas
    pages: list[int]
    evidence_excerpt: str
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

    # Verification + decision --------------------------------------------------
    verifier_scorecards: list[VerifierScorecard]
    verdict: Literal["auto_accept", "fixable", "sme_flag"] | None
    verdict_reason: str | None

    # Persistence + audit ------------------------------------------------------
    artifacts_gcs_prefix: str | None          # gs://.../extraction_outputs/phase1/<doc_id>/
    bigquery_extraction_row_id: str | None
    bigquery_run_row_id: str | None

    # Observability ------------------------------------------------------------
    otel_trace_id: str | None
    cost_usd: float                           # accumulated as agents run
    latency_ms: int                           # total wall-clock for the run
