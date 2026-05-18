"""
SciSpaCyMedicalNER — in-process Medical NER + Gemini Flash post-processor.

Two-stage:

  Stage 1 (sync, in-process):
      Load `en_ner_bionlp13cg_md`, `en_ner_bc5cdr_md`, `en_core_web_sm`
      once at module init (~3-5s cold-start, then cached). For each page,
      run each model and collect raw entities with offsets + source model.

  Stage 2 (async, one Gemini Flash @ T=0.0 call per doc):
      Pass the raw entities + per-page text + Block Profiler tags to the
      `medical_ner.j2` prompt. Gemini:
        - categorizes each entity into one of the 4 schema-v2 umbrellas
          (or `drop` if it's a false-positive / in a refs / fax-noise block)
        - deduplicates repeated mentions across pages
        - annotates report_metadata candidates with target_field_hint

Models are loaded lazily on first call. Subsequent calls reuse the cached
spaCy `Language` instances. The Stage 1 invocation is wrapped in
`asyncio.to_thread()` so it doesn't block the LangGraph event loop.

The output is `ParserHypothesis` (see core/state.py) — exactly the shape the
Coverage Auditors consume as their recall safety net.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.errors import MedicalNERError
from core.observability import trace
from core.prompt_renderer import PromptRenderer
from core.state import BlockProfile, PageText, ParserHypothesis, ParserHypothesisCandidate

logger = logging.getLogger(__name__)


SCISPACY_MODELS: tuple[str, ...] = (
    "en_ner_bionlp13cg_md",
    "en_ner_bc5cdr_md",
)
GENERAL_MODELS: tuple[str, ...] = (
    "en_core_web_sm",
)


TargetUmbrella = Literal[
    "report_metadata",
    "Genomic_Variant_umbrella",
    "other_molecular_biomarker_umbrella",
    "tested_biomarker_umbrella",
    "drop",
]


# ---------------------------------------------------------------------------
# Structured-output schema for the Gemini post-processor
# ---------------------------------------------------------------------------


class _CandidateOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    target_umbrella: TargetUmbrella
    target_field_hint: str | None = None
    pages: list[int] = Field(default_factory=list)
    evidence_excerpt: str = ""
    source_models: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""


class _DroppedExampleOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    drop_reason: str


class ParserHypothesisOutput(BaseModel):
    """Top-level structured output of the post-processor Gemini call."""

    model_config = ConfigDict(extra="forbid")
    doc_id: str
    candidates: list[_CandidateOut] = Field(default_factory=list)
    counts_by_umbrella: dict[str, int] = Field(default_factory=dict)
    dropped_count: int = 0
    dropped_examples: list[_DroppedExampleOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# SciSpaCyMedicalNER
# ---------------------------------------------------------------------------


class SciSpaCyMedicalNER:
    """In-process spaCy + SciSpaCy raw extractor + Gemini Flash post-processor.

    Lazy-loads models on first `extract_raw_entities` call. Tests can
    monkeypatch `_load_models` to skip the real spaCy load.
    """

    def __init__(
        self,
        *,
        prompt_renderer: PromptRenderer,
        model_name: str | None = None,
        temperature: float | None = None,
    ) -> None:
        self._renderer = prompt_renderer
        self._llm_model_name = model_name or os.environ.get(
            "GEMINI_FLASH_MODEL", "gemini-2.5-flash"
        )
        self._temperature = float(
            temperature if temperature is not None
            else os.environ.get("GEMINI_TEMPERATURE", "0.0")
        )
        self._nlp_pipelines: dict[str, Any] | None = None
        self._load_lock = asyncio.Lock()

    # -----------------------------------------------------------------------
    # Stage 1 — sync spaCy extraction wrapped in asyncio.to_thread
    # -----------------------------------------------------------------------

    @trace("preprocess.medical_ner.extract_raw_entities")
    async def extract_raw_entities(
        self,
        *,
        doc_id: str,
        pages: list[PageText],
    ) -> list[dict[str, Any]]:
        """Run all 3 spaCy/SciSpaCy pipelines across every page.

        Returns a flat list of raw entity dicts: {text, label, page,
        char_start, char_end, source_model}. The Gemini post-processor
        consumes this list as its input.
        """
        await self._ensure_loaded(doc_id=doc_id)

        def _do_extract() -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            assert self._nlp_pipelines is not None
            for p in pages:
                page_number = p.get("page_number") or 1
                text = p.get("text", "") or ""
                if not text.strip():
                    continue
                for model_name, nlp in self._nlp_pipelines.items():
                    try:
                        doc = nlp(text)
                    except Exception as exc:
                        logger.warning(
                            "medical_ner: %s failed on page %d: %s",
                            model_name, page_number, exc,
                        )
                        continue
                    for ent in doc.ents:
                        out.append({
                            "text": ent.text,
                            "label": ent.label_,
                            "page": page_number,
                            "char_start": int(ent.start_char),
                            "char_end": int(ent.end_char),
                            "source_model": model_name,
                        })
            return out

        try:
            entities = await asyncio.to_thread(_do_extract)
        except Exception as exc:
            raise MedicalNERError(
                f"spaCy raw extraction failed: {exc}",
                doc_id=doc_id,
                retry_safe=False,
                context={"error": str(exc)},
            ) from exc

        logger.info(
            "medical_ner: doc_id=%s extracted %d raw entit(ies) across %d page(s)",
            doc_id, len(entities), len(pages),
        )
        return entities

    async def _ensure_loaded(self, *, doc_id: str) -> None:
        if self._nlp_pipelines is not None:
            return
        async with self._load_lock:
            if self._nlp_pipelines is not None:
                return
            try:
                self._nlp_pipelines = await asyncio.to_thread(self._load_models)
            except Exception as exc:
                raise MedicalNERError(
                    f"Failed to load spaCy/SciSpaCy models: {exc}. "
                    f"Run `make verify` to confirm the model wheels installed.",
                    doc_id=doc_id,
                    retry_safe=False,
                    context={"error": str(exc)},
                ) from exc

    def _load_models(self) -> dict[str, Any]:
        """Synchronous load of all 3 models. Called once via to_thread."""
        import spacy

        pipelines: dict[str, Any] = {}
        for name in SCISPACY_MODELS + GENERAL_MODELS:
            logger.info("medical_ner: loading model %s ...", name)
            pipelines[name] = spacy.load(name)
        logger.info("medical_ner: %d model(s) loaded.", len(pipelines))
        return pipelines

    # -----------------------------------------------------------------------
    # Stage 2 — Gemini Flash post-processor
    # -----------------------------------------------------------------------

    @trace("preprocess.medical_ner.post_process")
    async def post_process(
        self,
        *,
        doc_id: str,
        pages: list[PageText],
        block_profiles: list[BlockProfile],
        raw_entities: list[dict[str, Any]],
    ) -> ParserHypothesis:
        """Run the Gemini Flash post-processor that turns raw spaCy entities
        into the typed ParserHypothesis. Returns an empty hypothesis (with
        all umbrella counts at zero) if there are no raw entities."""
        if not raw_entities:
            logger.info(
                "medical_ner: doc_id=%s no raw entities — returning empty hypothesis",
                doc_id,
            )
            return ParserHypothesis(
                doc_id=doc_id,
                candidates=[],
                counts_by_umbrella={
                    "report_metadata": 0,
                    "Genomic_Variant_umbrella": 0,
                    "other_molecular_biomarker_umbrella": 0,
                    "tested_biomarker_umbrella": 0,
                },
                dropped_count=0,
                dropped_examples=[],
            )

        # Attach block_profiles to the page payload the prompt expects.
        profiles_by_page: dict[int, list[dict[str, Any]]] = {}
        for bp in block_profiles:
            profiles_by_page.setdefault(bp.get("page_number") or 1, []).append({
                "block_id": bp.get("block_id"),
                "text_role": bp.get("text_role"),
                "target_umbrella_hint": bp.get("target_umbrella_hint"),
            })

        pages_for_prompt = [
            {
                "page_number": p.get("page_number"),
                "text": (p.get("text", "") or "")[:4000],   # cap per page
                "block_profiles_on_page": profiles_by_page.get(
                    p.get("page_number") or 1, []
                ),
            }
            for p in pages
        ]

        prompt = self._renderer.render_medical_ner_prompt(
            doc_id=doc_id,
            pages=pages_for_prompt,
            raw_scispacy_entities=raw_entities,
        )

        try:
            llm_out = await self._invoke_llm(prompt)
        except Exception as exc:
            raise MedicalNERError(
                f"Gemini post-processor call failed: {exc}",
                doc_id=doc_id,
                retry_safe=True,
                context={"model": self._llm_model_name, "error": str(exc)},
            ) from exc

        # Sanity: counts_by_umbrella sums to len(candidates) on non-drop entries.
        if llm_out.counts_by_umbrella:
            counted = sum(
                v for k, v in llm_out.counts_by_umbrella.items() if k != "drop"
            )
            if counted != len(llm_out.candidates):
                logger.warning(
                    "medical_ner: counts_by_umbrella sums to %d but emitted %d "
                    "candidates — normalizing (Gemini rounding error?).",
                    counted, len(llm_out.candidates),
                )

        candidates: list[ParserHypothesisCandidate] = [
            ParserHypothesisCandidate(
                text=c.text,
                target_umbrella=c.target_umbrella,  # type: ignore[arg-type]
                target_field_hint=c.target_field_hint,
                pages=c.pages,
                evidence_excerpt=c.evidence_excerpt,
                source_models=c.source_models,
                confidence=c.confidence,
                rationale=c.rationale,
            )
            for c in llm_out.candidates
            if c.target_umbrella != "drop"
        ]

        hypothesis = ParserHypothesis(
            doc_id=doc_id,
            candidates=candidates,
            counts_by_umbrella={
                "report_metadata": sum(1 for c in candidates if c["target_umbrella"] == "report_metadata"),
                "Genomic_Variant_umbrella": sum(1 for c in candidates if c["target_umbrella"] == "Genomic_Variant_umbrella"),
                "other_molecular_biomarker_umbrella": sum(1 for c in candidates if c["target_umbrella"] == "other_molecular_biomarker_umbrella"),
                "tested_biomarker_umbrella": sum(1 for c in candidates if c["target_umbrella"] == "tested_biomarker_umbrella"),
            },
            dropped_count=llm_out.dropped_count,
            dropped_examples=[{"text": e.text, "drop_reason": e.drop_reason}
                              for e in llm_out.dropped_examples],
        )

        logger.info(
            "medical_ner: doc_id=%s post-processed → %d candidate(s) "
            "(metadata=%d, variants=%d, biomarkers=%d, panel=%d), dropped=%d",
            doc_id,
            len(candidates),
            hypothesis["counts_by_umbrella"].get("report_metadata", 0),
            hypothesis["counts_by_umbrella"].get("Genomic_Variant_umbrella", 0),
            hypothesis["counts_by_umbrella"].get("other_molecular_biomarker_umbrella", 0),
            hypothesis["counts_by_umbrella"].get("tested_biomarker_umbrella", 0),
            hypothesis["dropped_count"],
        )
        return hypothesis

    async def _invoke_llm(self, prompt: str) -> ParserHypothesisOutput:
        from langchain_google_genai import ChatGoogleGenerativeAI

        llm = ChatGoogleGenerativeAI(
            model=self._llm_model_name,
            temperature=self._temperature,
        )
        structured = llm.with_structured_output(
            ParserHypothesisOutput, method="function_calling"
        )
        result: Any = await structured.ainvoke(prompt)
        if isinstance(result, ParserHypothesisOutput):
            return result
        return ParserHypothesisOutput.model_validate(result)

    # -----------------------------------------------------------------------
    # Convenience: run both stages
    # -----------------------------------------------------------------------

    @trace("preprocess.medical_ner.run")
    async def run(
        self,
        *,
        doc_id: str,
        pages: list[PageText],
        block_profiles: list[BlockProfile],
    ) -> ParserHypothesis:
        """End-to-end: spaCy extraction → Gemini post-processor → ParserHypothesis."""
        raw = await self.extract_raw_entities(doc_id=doc_id, pages=pages)
        return await self.post_process(
            doc_id=doc_id,
            pages=pages,
            block_profiles=block_profiles,
            raw_entities=raw,
        )
