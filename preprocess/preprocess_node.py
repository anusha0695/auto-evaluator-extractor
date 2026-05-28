"""
preprocess_node — the LangGraph node that wires the preprocessing layer.

Sequential where mandatory, parallel where independent:

    DocAIParser.parse(gcs_uri)          [sequential]      → DocProfile
                  │
                  ▼
    FaxHeaderFilter.filter(pages,...)   [sequential]      → cleaned pages + flagged blocks
                  │
                  ├──── BlockProfiler.profile()  ─┐       [parallel — asyncio.gather]
                  │                               │
                  └──── SciSpaCyMedicalNER.       ─┤
                            extract_raw_entities()│
                                                  │
                  ┌───────────────────────────────┘
                  ▼
    SciSpaCyMedicalNER.post_process()    [sequential]    → ParserHypothesis
                  │
                  ▼
    persistence.write_artifact(block_profiles)            [persisted to GCS]
    persistence.write_artifact(parser_hypothesis)         [persisted to GCS]
                  │
                  ▼
    return PipelineState (doc_profile + parser_hypothesis populated)

All-or-nothing: any preprocessing-layer error bubbles up as a
`PreprocessingError` and the runner routes the doc to SME (we never proceed
with partial state — the downstream agents would emit garbage).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from core.errors import PipelineError, PreprocessingError
from core.observability import span, trace
from core.persistence import Persistence
from core.state import (
    BlockProfile,
    DocProfile,
    PageText,
    ParserHypothesis,
    PipelineState,
)
from preprocess.bbox_synthesizer import BboxSynthesizer
from preprocess.block_profiler import BlockProfiler
from preprocess.docai_parser import DocAIParser
from preprocess.fax_header_filter import FaxHeaderFilter
from preprocess.medical_ner import SciSpaCyMedicalNER

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreprocessNodeDependencies:
    """The four preprocessing components, injected once at runner boot.

    Lets `preprocess_node()` be called as a plain function from inside the
    LangGraph state graph while keeping the components testable / swappable.
    """

    docai_parser: DocAIParser
    fax_filter: FaxHeaderFilter
    block_profiler: BlockProfiler
    medical_ner: SciSpaCyMedicalNER
    persistence: Persistence
    bbox_synthesizer: BboxSynthesizer | None = None


# ---------------------------------------------------------------------------
# The node
# ---------------------------------------------------------------------------


def make_preprocess_node(deps: PreprocessNodeDependencies):
    """Return an async node function bound to the given dependencies.

    Usage in pipeline/graph_v1.py:

        deps = PreprocessNodeDependencies(...)
        node = make_preprocess_node(deps)
        graph.add_node("preprocess_node", node)
    """

    @trace("preprocess.preprocess_node")
    async def preprocess_node(state: PipelineState) -> dict:
        doc_id = state.get("doc_id") or "unknown"
        gcs_uri = state.get("gcs_uri")
        raw_bytes = state.get("raw_pdf_bytes")
        if not gcs_uri and not raw_bytes:
            raise PreprocessingError(
                "preprocess_node: neither state.gcs_uri nor state.raw_pdf_bytes is set. "
                "Provide one — gcs_uri for production / batch, raw_pdf_bytes for local dev.",
                doc_id=doc_id,
                retry_safe=False,
            )

        source_label = gcs_uri or f"<inline {len(raw_bytes or b'')}-byte PDF>"
        t0 = time.monotonic()
        with span("preprocess.preprocess_node", {"doc_id": doc_id, "source": source_label}):
            # ----- 1. DocAI Layout Parser -------------------------------
            if gcs_uri:
                doc_profile, raw_uri = await deps.docai_parser.parse(doc_id, gcs_uri)
            else:
                doc_profile, raw_uri = await deps.docai_parser.parse(
                    doc_id, raw_pdf_bytes=raw_bytes,
                )
            logger.info(
                "preprocess_node: doc_id=%s source=%s DocAI parsed %d page(s) / %d block(s); raw=%s",
                doc_id,
                source_label,
                doc_profile.get("total_pages"),
                len(doc_profile.get("blocks") or []),
                raw_uri,
            )

            # ----- 1b. Bbox synthesis fallback ---------------------------
            # The pinned Layout Parser v1.6 processor doesn't emit
            # bounding_box even with return_bounding_boxes=True. If every
            # block lacks a bbox and we have the raw PDF bytes locally,
            # synthesize bboxes from pdfplumber word positions so the UI
            # can render overlays. No-op when bboxes are already present.
            if deps.bbox_synthesizer is not None and raw_bytes:
                blocks_list = list(doc_profile.get("blocks") or [])
                missing = sum(1 for b in blocks_list if not (b.get("bbox") or []))
                if missing:
                    try:
                        synthesized = deps.bbox_synthesizer.synthesize(
                            raw_bytes, blocks_list,
                        )
                        doc_profile["blocks"] = list(synthesized)
                    except Exception as exc:
                        # Non-fatal: UI overlays are a nice-to-have, not
                        # a correctness requirement for extraction.
                        logger.warning(
                            "BboxSynthesizer failed for doc_id=%s (%s) — "
                            "continuing without overlays.", doc_id, exc,
                        )

            # ----- 2. Fax-header filter (deterministic) -----------------
            fax_result = deps.fax_filter.filter(
                pages=list(doc_profile.get("pages") or []),
                blocks=list(doc_profile.get("blocks") or []),
            )
            doc_profile["pages"] = list(fax_result.pages)
            doc_profile["fax_header_blocks_removed"] = fax_result.blocks_flagged

            # ----- 3 & 4. Block Profiler ∥ SciSpaCy raw extraction -----
            #     (Deterministic NER post-processor is sequential after both
            #      finish so it can see block_profiles to resolve umbrellas
            #      and drop entities in fax-noise / reference blocks.)
            try:
                profiles_task = deps.block_profiler.profile(
                    doc_id=doc_id,
                    total_pages=doc_profile.get("total_pages") or 1,
                    blocks=list(doc_profile.get("blocks") or []),
                    fax_noise_block_ids=fax_result.fax_noise_block_ids,
                )
                ner_raw_task = deps.medical_ner.extract_raw_entities(
                    doc_id=doc_id,
                    pages=list(doc_profile.get("pages") or []),
                )
                block_profiles, raw_entities = await asyncio.gather(
                    profiles_task, ner_raw_task
                )
            except PipelineError:
                raise
            except Exception as exc:
                raise PreprocessingError(
                    f"Parallel BlockProfiler + Medical NER stage failed: {exc}",
                    doc_id=doc_id,
                    retry_safe=True,
                    context={"error": str(exc)},
                ) from exc

            doc_profile["block_profiles"] = list(block_profiles)

            # ----- 5. Medical NER post-processor (deterministic) --------
            parser_hypothesis = await deps.medical_ner.post_process(
                doc_id=doc_id,
                pages=list(doc_profile.get("pages") or []),
                block_profiles=list(block_profiles),
                raw_entities=raw_entities,
            )

            # ----- 6. Cache outputs to GCS ------------------------------
            # `blocks` (bbox + text + page + section_path) is persisted as a
            # clean artifact so the UI can render overlays by block_id without
            # re-walking the nested docai_raw.json proto. `pages` (with
            # block_spans) is persisted too for completeness.
            await asyncio.gather(
                deps.persistence.write_artifact(
                    doc_id, "block_profiles", list(block_profiles)
                ),
                deps.persistence.write_artifact(
                    doc_id, "parser_hypothesis", dict(parser_hypothesis)
                ),
                deps.persistence.write_artifact(
                    doc_id, "blocks", list(doc_profile.get("blocks") or [])
                ),
                deps.persistence.write_artifact(
                    doc_id, "pages", list(doc_profile.get("pages") or [])
                ),
                # M8a: OCR token geometry for the SME UI's pixel-tight highlight
                # (may be empty — the UI then falls back to block-level boxes).
                deps.persistence.write_artifact(
                    doc_id, "word_geometry", list(doc_profile.get("word_geometry") or [])
                ),
            )

            # Bundle the source PDF into the doc's artifact folder so the UI
            # is self-contained (artifacts/<doc_id>/source.pdf). LOCAL backend
            # only + only when we have the raw bytes (local-pdf runs): we must
            # not auto-copy PHI into the GCS artifacts bucket — for prod the
            # source already lives at its gcs_uri.
            if raw_bytes and getattr(deps.persistence, "backend", "local") == "local":
                try:
                    await deps.persistence.write_artifact(
                        doc_id, "source", raw_bytes, ext="pdf",
                    )
                except Exception as exc:
                    logger.warning(
                        "preprocess_node: could not persist source.pdf for "
                        "doc_id=%s (%s) — UI will fall back to folder search.",
                        doc_id, exc,
                    )

        latency_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "preprocess_node: doc_id=%s complete in %dms "
            "(blocks=%d, fax_noise=%d, ner_candidates=%d)",
            doc_id,
            latency_ms,
            len(doc_profile.get("blocks") or []),
            fax_result.blocks_flagged,
            len(parser_hypothesis.get("candidates") or []),
        )

        # Trace: one record per preprocess agent so the field timeline starts at the
        # very beginning — DocAI → fax filter → BlockProfiler (per-block role) → NER.
        from core.trace_recorder import extend_trace, record
        n_blocks = len(doc_profile.get("blocks") or [])
        n_profiles = len(doc_profile.get("block_profiles") or [])
        n_candidates = len(parser_hypothesis.get("candidates") or [])
        from collections import Counter as _Counter
        role_counts = _Counter(
            str(bp.get("text_role") or "?")
            for bp in (doc_profile.get("block_profiles") or [])
            if isinstance(bp, dict))
        section_counts = _Counter(
            t for bp in (doc_profile.get("block_profiles") or [])
            if isinstance(bp, dict)
            for t in (bp.get("target_umbrella_hints") or []))
        cand_section_counts = _Counter(
            str(c.get("target_umbrella") or "?")
            for c in (parser_hypothesis.get("candidates") or [])
            if isinstance(c, dict))
        recs = [
            record(phase="preprocess", agent="DocAIParser",
                   plain="Our system parsed the PDF into pages, blocks, and text we can reason over.",
                   input_summary=f"source={source_label}",
                   output_summary=f"{len(doc_profile.get('pages') or [])} page(s), {n_blocks} block(s)",
                   verdict="parsed"),
            record(phase="preprocess", agent="FaxHeaderFilter",
                   plain="Our system flagged fax-transport noise (headers/banners) so it's ignored downstream.",
                   input_summary=f"{n_blocks} block(s)",
                   output_summary=f"flagged {fax_result.blocks_flagged} fax-noise block(s)",
                   verdict="filtered"),
            record(phase="preprocess", agent="BlockProfiler",
                   plain="Our system labelled each block of text with a role and routed it to the right section(s).",
                   input_summary=f"{n_blocks} block(s) → roles + section hints",
                   output_summary=(f"{n_profiles} profile(s); roles={dict(role_counts.most_common(6))}; "
                                   f"hint-sections={dict(section_counts.most_common(6))}"),
                   verdict="profiled"),
            record(phase="preprocess", agent="MedicalNER",
                   plain="Our system spotted clinical entities (genes, dates, IDs) in the source text and proposed candidates for each section.",
                   input_summary=f"{n_blocks} block(s) over 3 SciSpaCy model(s)",
                   output_summary=(f"{n_candidates} candidate(s); by section={dict(cand_section_counts.most_common(6))}"),
                   verdict="extracted"),
        ]
        return {
            "doc_profile": doc_profile,
            "parser_hypothesis": parser_hypothesis,
            "artifacts_gcs_prefix": deps.persistence.config.doc_prefix(doc_id),
            # latency tracking — additive
            "latency_ms": int(state.get("latency_ms", 0)) + latency_ms,
            "agent_trace": extend_trace(state.get("agent_trace"), *recs),
        }

    return preprocess_node


# ---------------------------------------------------------------------------
# Stand-alone helpers (useful in tests)
# ---------------------------------------------------------------------------


async def run_preprocess(
    state: PipelineState,
    deps: PreprocessNodeDependencies,
) -> dict:
    """Direct invocation (no LangGraph wrapper) — convenient for tests."""
    node = make_preprocess_node(deps)
    return await node(state)
