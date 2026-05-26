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

import asyncio
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
    "clinical_significance",
    # P3-M1: surgical-pathology + clinical narrative roles (give the recall floor
    # block-roles to map onto significant_findings / clinical_information).
    "final_diagnosis", "gross_description", "microscopic_description",
    "synoptic_report", "clinical_history",
    "addendum", "references", "cpt_codes",
    "electronic_signature", "page_header", "page_footer", "fax_transport_noise",
    "chart_image_caption", "disclaimer_or_notes", "other",
]

TargetUmbrellaHint = Literal[
    "report_metadata",
    "other_molecular_biomarker_umbrella",
    "tested_biomarker_umbrella",
    # Phase 2 (schema v3):
    "significant_findings",
    "clinical_information",
    "none",
]


class BlockProfileItem(BaseModel):
    """One classification — matches the JSON shape Gemini emits per block.

    R2: `target_umbrella_hints` is a LIST. A block can feed more than one
    umbrella (a results_table holds variant rows AND panel membership).
    """

    model_config = ConfigDict(extra="forbid")

    block_id: str
    page_number: int = Field(ge=1)
    text_role: TextRole
    target_umbrella_hints: list[TargetUmbrellaHint] = Field(min_length=1)
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
        max_blocks_per_call: int = 300,
        max_concurrent_batches: int = 3,
    ) -> None:
        self._renderer = prompt_renderer
        self._model_name = model_name or os.environ.get(
            "GEMINI_FLASH_MODEL", "gemini-2.5-flash"
        )
        self._temperature = float(
            temperature if temperature is not None
            else os.environ.get("GEMINI_TEMPERATURE", "0.0")
        )
        # Batch size: max blocks per Gemini call. Larger prompts trigger
        # safety filters / refusals / truncation more often (we saw exactly
        # this when DocAI's default processor started returning richer
        # output). 300 keeps each call comfortably under any soft limits.
        self._max_blocks_per_call = max_blocks_per_call
        # Parallelism: how many Gemini batches can run concurrently. 3 is
        # a safe default that avoids Vertex rate limits while still
        # finishing a multi-hundred-block doc in ~1 round-trip duration.
        self._max_concurrent_batches = max_concurrent_batches

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
                target_umbrella_hints=["none"],
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

        # ----- Split into batches ------------------------------------------
        # Each batch is a self-contained Gemini call with its own prompt
        # + structured output. Per-batch 1:1 validation; concatenate at end.
        batches: list[list[BlockInfo]] = [
            remaining_blocks[i : i + self._max_blocks_per_call]
            for i in range(0, len(remaining_blocks), self._max_blocks_per_call)
        ]
        logger.info(
            "BlockProfiler: doc_id=%s splitting %d blocks into %d batch(es) "
            "of ≤%d blocks each (max %d concurrent).",
            doc_id, len(remaining_blocks), len(batches),
            self._max_blocks_per_call, self._max_concurrent_batches,
        )

        # ----- Fan out batches in parallel under a semaphore ---------------
        semaphore = asyncio.Semaphore(self._max_concurrent_batches)

        async def _run_one_batch(
            batch_idx: int, batch: list[BlockInfo],
        ) -> list[BlockProfile]:
            async with semaphore:
                batch_prompt = self._renderer.render_block_profiler_prompt(
                    doc_id=doc_id,
                    total_pages=total_pages,
                    blocks=[self._block_for_prompt(b) for b in batch],
                )
                try:
                    batch_out = await self._invoke_llm(batch_prompt)
                except Exception as exc:
                    raise BlockProfilerError(
                        f"Gemini Block Profiler batch "
                        f"{batch_idx + 1}/{len(batches)} failed: {exc}",
                        doc_id=doc_id,
                        retry_safe=True,
                        context={
                            "model": self._model_name,
                            "batch_idx": batch_idx,
                            "batch_size": len(batch),
                            "error": str(exc),
                        },
                    ) from exc

                # Per-batch 1:1 sanity check.
                if len(batch_out.items) != len(batch):
                    raise BlockProfilerError(
                        f"BlockProfiler batch {batch_idx + 1} returned "
                        f"{len(batch_out.items)} items for {len(batch)} "
                        "blocks (must match 1:1)",
                        doc_id=doc_id, retry_safe=True,
                        context={
                            "batch_idx": batch_idx,
                            "got": len(batch_out.items),
                            "expected": len(batch),
                        },
                    )

                expected_ids = {b["block_id"] for b in batch}
                got_ids = {item.block_id for item in batch_out.items}
                if expected_ids != got_ids:
                    raise BlockProfilerError(
                        f"BlockProfiler batch {batch_idx + 1} block_id set "
                        "diverges from input",
                        doc_id=doc_id, retry_safe=True,
                        context={
                            "batch_idx": batch_idx,
                            "missing": sorted(expected_ids - got_ids),
                            "extra": sorted(got_ids - expected_ids),
                        },
                    )

                return [
                    BlockProfile(
                        block_id=item.block_id,
                        page_number=item.page_number,
                        text_role=item.text_role,
                        target_umbrella_hints=list(item.target_umbrella_hints),
                        confidence=item.confidence,
                        rationale=item.rationale,
                    )
                    for item in batch_out.items
                ]

        batch_results: list[list[BlockProfile]] = await asyncio.gather(
            *[_run_one_batch(i, b) for i, b in enumerate(batches)]
        )
        gemini_profiles: list[BlockProfile] = [
            p for batch_profiles in batch_results for p in batch_profiles
        ]

        # Cross-batch global 1:1 check — defensive (each batch already
        # validated). Catches any concatenation bug or batch overlap.
        if len(gemini_profiles) != len(remaining_blocks):
            raise BlockProfilerError(
                f"BlockProfiler: concatenated {len(gemini_profiles)} "
                f"profiles for {len(remaining_blocks)} blocks across "
                f"{len(batches)} batches",
                doc_id=doc_id, retry_safe=True,
            )

        merged = self._merge_in_input_order(blocks, pre_tagged, gemini_profiles)
        logger.info(
            "BlockProfiler: doc_id=%s classified %d block(s) "
            "(%d pre-tagged fax-noise, %d via Gemini Flash in %d batch(es))",
            doc_id,
            len(merged),
            len(pre_tagged),
            len(gemini_profiles),
            len(batches),
        )
        return merged

    # -----------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------

    async def _invoke_llm(self, prompt: str) -> BlockProfilerOutput:
        """Run the Gemini Flash call. Uses langchain-google-genai's unified
        wrapper — backend selected by GOOGLE_GENAI_USE_VERTEXAI env var.

        Uses method="json_mode" (NOT function_calling). Function calling
        lets Gemini decide whether to invoke the function; on big or
        ambiguous prompts the model sometimes refuses to fire and we get
        a silent None. json_mode forces responseMimeType=application/json
        — the schema is embedded in the prompt template and the model
        must emit JSON matching it.
        """
        from langchain_google_genai import ChatGoogleGenerativeAI

        # max_output_tokens: a 300-block batch emits 300 BlockProfileItem
        # objects (~6 fields each); the default ~8K output ceiling can
        # truncate mid-array and produce JSON that fails validation.
        # Set to the model max (Gemini 2.5 Flash = 65536) so the limit
        # never bites — we'd rather pay for tokens than silently lose
        # blocks from the classification.
        llm = ChatGoogleGenerativeAI(
            model=self._model_name,
            temperature=self._temperature,
            max_output_tokens=65536,
        )
        structured = llm.with_structured_output(
            BlockProfilerOutput, method="json_mode",
        )
        result: Any = await structured.ainvoke(prompt)
        if isinstance(result, BlockProfilerOutput):
            return result
        if result is None:
            raise BlockProfilerError(
                "Gemini returned None even with response_schema enabled — "
                "likely truncated mid-JSON or blocked. "
                f"model={self._model_name!r}, prompt={len(prompt)} chars.",
                retry_safe=False,
                context={"model": self._model_name, "prompt_chars": len(prompt)},
            )
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
