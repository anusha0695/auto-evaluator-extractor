"""
BlockProfiler — Gemini 2.5 Flash @ T=0.0 semantic block classifier.

Takes the DocAI layout blocks (post fax-header filtering) and assigns each one
a `text_role` from the closed vocabulary in
`config/prompts/preprocess/block_profiler.j2`, plus a `target_umbrella_hint`
identifying which schema-v2 umbrella section is most likely to extract from
that block.

One Gemini Flash call per document; structured output via langchain-google-genai
4.2.2's `with_structured_output` API. Temperature is locked at 0.0 (no
nondeterminism in the layout taxonomy).

The wrapper auto-selects backend:
  - GOOGLE_GENAI_USE_VERTEXAI=true  → Vertex AI Gemini, BAA-covered.
  - GOOGLE_GENAI_USE_VERTEXAI=false → Google AI Studio (dev only).

Honors the fax-header filter: if `fax_noise_block_ids` is provided, those
blocks are pre-tagged `text_role: fax_transport_noise` deterministically and
not sent to Gemini (saves tokens + guarantees correctness on the band).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.errors import BlockProfilerError
from core.observability import trace
from core.prompt_renderer import PromptRenderer
from core.state import BlockInfo, BlockProfile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structured-output schema (matches the closed vocab in block_profiler.j2)
# ---------------------------------------------------------------------------


TextRole = Literal[
    "report_title", "vendor_branding", "practice_block", "patient_demographics",
    "specimen_metadata", "ordering_provider", "accession_block",
    "panel_or_test_name", "methodology", "results_table", "interpretation",
    "clinical_significance", "references", "cpt_codes", "electronic_signature",
    "page_header", "page_footer", "fax_transport_noise", "chart_image_caption",
    "disclaimer_or_notes", "other",
]

TargetUmbrellaHint = Literal[
    "report_metadata",
    "Genomic_Variant_umbrella",
    "other_molecular_biomarker_umbrella",
    "tested_biomarker_umbrella",
    "none",
]


class BlockProfileItem(BaseModel):
    """One classification — matches the JSON shape Gemini emits per block."""

    model_config = ConfigDict(extra="forbid")

    block_id: str
    page_number: int = Field(ge=1)
    text_role: TextRole
    target_umbrella_hint: TargetUmbrellaHint
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


class BlockProfilerOutput(BaseModel):
    """Top-level structured-output schema for the Gemini call."""

    model_config = ConfigDict(extra="forbid")

    items: list[BlockProfileItem]


# ---------------------------------------------------------------------------
# BlockProfiler
# ---------------------------------------------------------------------------


class BlockProfiler:
    """Classifies every DocAI block via one Gemini 2.5 Flash call."""

    def __init__(
        self,
        *,
        prompt_renderer: PromptRenderer,
        model_name: str | None = None,
        temperature: float | None = None,
        max_blocks_per_call: int = 400,
    ) -> None:
        self._renderer = prompt_renderer
        self._model_name = model_name or os.environ.get(
            "GEMINI_FLASH_MODEL", "gemini-2.5-flash"
        )
        self._temperature = float(
            temperature if temperature is not None
            else os.environ.get("GEMINI_TEMPERATURE", "0.0")
        )
        # Safety net — typical reports have <100 blocks, but we cap so a
        # pathological doc doesn't blow the context window in one shot.
        self._max_blocks_per_call = max_blocks_per_call

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    @trace("preprocess.block_profiler.profile")
    async def profile(
        self,
        *,
        doc_id: str,
        total_pages: int,
        blocks: list[BlockInfo],
        fax_noise_block_ids: set[str] | None = None,
    ) -> list[BlockProfile]:
        """Run the classifier. Returns one BlockProfile per input block."""
        fax_noise = fax_noise_block_ids or set()

        # Pre-tag fax-noise blocks deterministically. Don't send them to Gemini.
        pre_tagged: list[BlockProfile] = [
            BlockProfile(
                block_id=b["block_id"],
                page_number=b.get("page_number") or 1,
                text_role="fax_transport_noise",
                target_umbrella_hint="none",
                confidence=1.0,
                rationale="Pre-tagged by FaxHeaderFilter deterministic regex.",
            )
            for b in blocks
            if b.get("block_id") in fax_noise
        ]
        remaining_blocks = [b for b in blocks if b.get("block_id") not in fax_noise]

        if not remaining_blocks:
            logger.info(
                "BlockProfiler: doc_id=%s all blocks pre-tagged as fax noise — "
                "no Gemini call needed.",
                doc_id,
            )
            return pre_tagged

        # ----- Render the prompt -------------------------------------------
        prompt = self._renderer.render_block_profiler_prompt(
            doc_id=doc_id,
            total_pages=total_pages,
            blocks=[self._block_for_prompt(b) for b in remaining_blocks],
        )

        # ----- Invoke Gemini with structured output ------------------------
        try:
            llm_output = await self._invoke_llm(prompt)
        except Exception as exc:
            raise BlockProfilerError(
                f"Gemini Block Profiler call failed: {exc}",
                doc_id=doc_id,
                retry_safe=True,
                context={"model": self._model_name, "error": str(exc)},
            ) from exc

        # ----- Sanity checks -----------------------------------------------
        if len(llm_output.items) != len(remaining_blocks):
            raise BlockProfilerError(
                f"BlockProfiler returned {len(llm_output.items)} items for "
                f"{len(remaining_blocks)} blocks (must match 1:1)",
                doc_id=doc_id,
                retry_safe=True,
                context={
                    "got": len(llm_output.items),
                    "expected": len(remaining_blocks),
                },
            )

        expected_ids = {b["block_id"] for b in remaining_blocks}
        got_ids = {item.block_id for item in llm_output.items}
        if expected_ids != got_ids:
            missing = expected_ids - got_ids
            extra = got_ids - expected_ids
            raise BlockProfilerError(
                "BlockProfiler block_id set diverges from input",
                doc_id=doc_id,
                retry_safe=True,
                context={"missing": sorted(missing), "extra": sorted(extra)},
            )

        gemini_profiles: list[BlockProfile] = [
            BlockProfile(
                block_id=item.block_id,
                page_number=item.page_number,
                text_role=item.text_role,
                target_umbrella_hint=item.target_umbrella_hint,
                confidence=item.confidence,
                rationale=item.rationale,
            )
            for item in llm_output.items
        ]

        merged = self._merge_in_input_order(blocks, pre_tagged, gemini_profiles)
        logger.info(
            "BlockProfiler: doc_id=%s classified %d block(s) "
            "(%d pre-tagged fax-noise, %d via Gemini Flash)",
            doc_id,
            len(merged),
            len(pre_tagged),
            len(gemini_profiles),
        )
        return merged

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    async def _invoke_llm(self, prompt: str) -> BlockProfilerOutput:
        """Run the Gemini Flash call. Uses langchain-google-genai's unified
        wrapper — backend selected by GOOGLE_GENAI_USE_VERTEXAI env var."""
        from langchain_google_genai import ChatGoogleGenerativeAI

        llm = ChatGoogleGenerativeAI(
            model=self._model_name,
            temperature=self._temperature,
        )
        structured = llm.with_structured_output(BlockProfilerOutput, method="function_calling")
        result: Any = await structured.ainvoke(prompt)
        if isinstance(result, BlockProfilerOutput):
            return result
        # langchain may return a dict in some configs; coerce.
        return BlockProfilerOutput.model_validate(result)

    @staticmethod
    def _block_for_prompt(b: BlockInfo) -> dict[str, Any]:
        """Trim the block payload sent to Gemini — we don't ship the bbox
        coordinates (irrelevant for semantic classification, wastes tokens)."""
        text = b.get("text", "") or ""
        # Cap each block's text — long content blocks (interpretation
        # paragraphs) shouldn't dominate the prompt.
        if len(text) > 600:
            text = text[:600] + "…"
        return {
            "block_id": b.get("block_id"),
            "page_number": b.get("page_number"),
            "section_path": b.get("section_path"),
            "text": text,
        }

    @staticmethod
    def _merge_in_input_order(
        blocks: list[BlockInfo],
        pre_tagged: list[BlockProfile],
        gemini: list[BlockProfile],
    ) -> list[BlockProfile]:
        """Merge pre-tagged + Gemini-tagged profiles, preserving the original
        input block order."""
        by_id: dict[str, BlockProfile] = {}
        for p in pre_tagged:
            by_id[p["block_id"]] = p
        for p in gemini:
            by_id[p["block_id"]] = p
        return [by_id[b["block_id"]] for b in blocks if b.get("block_id") in by_id]
