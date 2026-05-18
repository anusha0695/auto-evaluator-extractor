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

            # ----- 2. Fax-header filter (deterministic) -----------------
            fax_result = deps.fax_filter.filter(
                pages=list(doc_profile.get("pages") or []),
                blocks=list(doc_profile.get("blocks") or []),
            )
            doc_profile["pages"] = list(fax_result.pages)
            doc_profile["fax_header_blocks_removed"] = fax_result.blocks_flagged

            # ----- 3 & 4. Block Profiler ∥ SciSpaCy raw extraction -----
            #     (Gemini post-processor is sequential after both finish so
            #      it can see block_profiles to drop entities in fax-noise /
            #      reference blocks.)
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

            # ----- 5. Medical NER post-processor (Gemini Flash) ---------
            parser_hypothesis = await deps.medical_ner.post_process(
                doc_id=doc_id,
                pages=list(doc_profile.get("pages") or []),
                block_profiles=list(block_profiles),
                raw_entities=raw_entities,
            )

            # ----- 6. Cache outputs to GCS ------------------------------
            await asyncio.gather(
                deps.persistence.write_artifact(
                    doc_id, "block_profiles", list(block_profiles)
                ),
                deps.persistence.write_artifact(
                    doc_id, "parser_hypothesis", dict(parser_hypothesis)
                ),
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

        # Return a state delta. LangGraph merges this into the state object.
        return {
            "doc_profile": doc_profile,
            "parser_hypothesis": parser_hypothesis,
            "artifacts_gcs_prefix": deps.persistence.config.doc_prefix(doc_id),
            # latency tracking — additive
            "latency_ms": int(state.get("latency_ms", 0)) + latency_ms,
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
