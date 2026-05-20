"""
SciSpaCyMedicalNER — in-process Medical NER + DETERMINISTIC post-processor.

Two-stage, NO LLM in stage 2:

  Stage 1 (sync, in-process):
      Load `en_ner_bionlp13cg_md`, `en_ner_bc5cdr_md`, `en_core_web_sm`
      once at module init (~3-5s cold-start, then cached). For each page,
      run each model on the full page text (so models see full sentence
      context), collect raw entities with offsets + source model, and
      attach each entity's source `block_id` via the page's block_spans
      (2C — exact offset→block attribution, no fuzzy matching).

  Stage 2 (sync, deterministic — was a Gemini Flash call):
      Apply the lookup tables in `config/ner_mapping.yaml`:
        - drop entities whose containing block role is in `drop_roles`
        - drop entities whose text is in the `stoplist`
        - map label → allowed umbrellas, intersect with the block's
          `target_umbrella_hints` (R2 multi-umbrella)
        - group by (normalize(text), umbrella), preserving every occurrence
        - assign target_field_hint for report_metadata via regex rules

Why deterministic: the LLM post-processor failed to scale (it stopped
deduplicating on long entity lists and truncated its JSON). The work is
pure bookkeeping with an exact-rules answer, and `parser_hypothesis` is only
a recall checklist for the Coverage Auditors — so a fast, deterministic,
testable mapper is strictly better. See config/ner_mapping.yaml.

The output is `ParserHypothesis` (see core/state.py) — exactly the shape the
Coverage Auditors consume as their recall safety net.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Literal

from core.errors import MedicalNERError
from core.observability import trace
from core.state import (
    BlockProfile,
    Occurrence,
    PageText,
    ParserHypothesis,
    ParserHypothesisCandidate,
)

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
]

# Real umbrellas (excludes the sentinel "none" used by block hints).
_REAL_UMBRELLAS = frozenset({
    "report_metadata",
    "Genomic_Variant_umbrella",
    "other_molecular_biomarker_umbrella",
    "tested_biomarker_umbrella",
})

_DEFAULT_MAPPING_PATH = "config/ner_mapping.yaml"


def _normalize_text(s: str) -> str:
    """Dedup key normalizer: collapse whitespace, lowercase, strip."""
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


# ---------------------------------------------------------------------------
# SciSpaCyMedicalNER
# ---------------------------------------------------------------------------


class SciSpaCyMedicalNER:
    """In-process spaCy + SciSpaCy raw extractor + deterministic mapper.

    Lazy-loads models on first `extract_raw_entities` call. Tests can
    monkeypatch `_load_models` to skip the real spaCy load. The mapping
    tables are loaded from `config/ner_mapping.yaml` at init.
    """

    def __init__(
        self,
        *,
        prompt_renderer: Any = None,    # accepted for back-compat; unused now
        mapping_path: str | Path | None = None,
        **_ignored: Any,                # tolerate legacy model_name/temperature kwargs
    ) -> None:
        self._nlp_pipelines: dict[str, Any] | None = None
        self._load_lock = asyncio.Lock()
        self._mapping = self._load_mapping(
            Path(mapping_path) if mapping_path else Path(_DEFAULT_MAPPING_PATH)
        )

    @staticmethod
    def _load_mapping(path: Path) -> dict[str, Any]:
        """Load and normalize the ner_mapping.yaml tables."""
        import yaml

        if not path.exists():
            raise MedicalNERError(
                f"NER mapping file not found: {path}. Expected "
                f"{_DEFAULT_MAPPING_PATH} relative to the repo root.",
                retry_safe=False,
            )
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return {
            "label_to_umbrellas": {
                k: list(v) for k, v in (raw.get("label_to_umbrellas") or {}).items()
            },
            "drop_roles": set(raw.get("drop_roles") or []),
            "stoplist": {str(s).strip().lower() for s in (raw.get("stoplist") or [])},
            "field_hint_rules": list(raw.get("field_hint_rules") or []),
            "vendor_names": [str(v).lower() for v in (raw.get("vendor_names") or [])],
        }

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
        char_start, char_end, source_model, block_id}. The deterministic
        post-processor consumes this list.

        `block_id` is resolved via the page's `block_spans` (2C): the block
        whose [start, end) range contains the entity's char_start. NER still
        runs on full page text (so models see sentence context); only the
        attribution is per-block.
        """
        await self._ensure_loaded(doc_id=doc_id)

        def _block_id_for_offset(spans: list[dict[str, Any]], offset: int) -> str | None:
            for s in spans:
                if s.get("start", 0) <= offset < s.get("end", 0):
                    return s.get("block_id")
            return None

        def _do_extract() -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            assert self._nlp_pipelines is not None
            for p in pages:
                page_number = p.get("page_number") or 1
                text = p.get("text", "") or ""
                if not text.strip():
                    continue
                spans = list(p.get("block_spans", []) or [])
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
                            "block_id": _block_id_for_offset(spans, int(ent.start_char)),
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
    # Stage 2 — DETERMINISTIC post-processor (no LLM)
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
        """Turn raw SciSpaCy entities into a typed ParserHypothesis using the
        deterministic lookup tables (config/ner_mapping.yaml). No LLM call.

        Steps per entity: drop by block-role / stoplist → label→umbrella(s)
        intersected with the block's hints → group by (norm_text, umbrella)
        preserving occurrences → field_hint for report_metadata.
        """
        if not raw_entities:
            logger.info(
                "medical_ner: doc_id=%s no raw entities — returning empty hypothesis",
                doc_id,
            )
            return self._empty_hypothesis(doc_id)

        # Index block_profiles by block_id for O(1) role/hint lookup.
        prof_by_id: dict[str, BlockProfile] = {
            bp.get("block_id", ""): bp for bp in block_profiles
        }
        # Index page text by page number for evidence-excerpt slicing.
        text_by_page: dict[int, str] = {
            (p.get("page_number") or 1): (p.get("text", "") or "") for p in pages
        }

        label_to_umbrellas: dict[str, list[str]] = self._mapping["label_to_umbrellas"]
        drop_roles: set[str] = self._mapping["drop_roles"]
        stoplist: set[str] = self._mapping["stoplist"]

        # group key = (normalize(text), umbrella) → aggregated candidate
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        dropped_count = 0
        dropped_examples: list[dict[str, str]] = []

        def _record_drop(text: str, reason: str) -> None:
            nonlocal dropped_count
            dropped_count += 1
            if len(dropped_examples) < 5:
                dropped_examples.append({"text": text, "drop_reason": reason})

        for ent in raw_entities:
            text = ent.get("text", "") or ""
            label = ent.get("label", "") or ""
            page = ent.get("page") or 1
            block_id = ent.get("block_id")
            prof = prof_by_id.get(block_id or "")
            text_role = prof.get("text_role", "") if prof else ""

            # --- drop gates ---
            if text_role in drop_roles:
                _record_drop(text, f"block role '{text_role}' in drop-set")
                continue
            if _normalize_text(text) in stoplist:
                _record_drop(text, "in stoplist (known false positive)")
                continue
            allowed = label_to_umbrellas.get(label)
            if not allowed:
                _record_drop(text, f"label '{label}' not in schema v2")
                continue

            # --- umbrella resolution (R2 intersection) ---
            block_hints = set(prof.get("target_umbrella_hints", []) if prof else [])
            block_hints &= _REAL_UMBRELLAS    # ignore the "none" sentinel
            allowed_set = set(allowed)
            resolved = allowed_set & block_hints
            if resolved:
                confidence = 0.9
            else:
                # Block didn't hint a compatible umbrella (or no profile) —
                # keep the label's umbrellas as a recall safety net.
                resolved = allowed_set
                confidence = 0.6

            excerpt = self._excerpt(
                text_by_page.get(page, ""), ent.get("char_start"), ent.get("char_end"),
            )

            for umbrella in sorted(resolved):
                key = (_normalize_text(text), umbrella)
                grp = groups.get(key)
                if grp is None:
                    grp = {
                        "text": text,
                        "target_umbrella": umbrella,
                        "occurrences": [],
                        "source_models": set(),
                        "confidence": confidence,
                        "block_confirmed": confidence >= 0.9,
                    }
                    groups[key] = grp
                grp["occurrences"].append(Occurrence(
                    block_id=block_id,
                    page=page,
                    char_start=ent.get("char_start"),
                    char_end=ent.get("char_end"),
                    text_role=text_role,
                    evidence_excerpt=excerpt,
                ))
                if ent.get("source_model"):
                    grp["source_models"].add(ent["source_model"])
                grp["confidence"] = max(grp["confidence"], confidence)
                grp["block_confirmed"] = grp["block_confirmed"] or confidence >= 0.9

        # --- materialize candidates ---
        candidates: list[ParserHypothesisCandidate] = []
        for grp in groups.values():
            occs = grp["occurrences"]
            pages_list = sorted({o.get("page") for o in occs if o.get("page")})
            field_hint = None
            if grp["target_umbrella"] == "report_metadata":
                field_hint = self._field_hint_for(grp["text"], occs, text_by_page)
            candidates.append(ParserHypothesisCandidate(
                text=grp["text"],
                target_umbrella=grp["target_umbrella"],  # type: ignore[arg-type]
                target_field_hint=field_hint,
                occurrences=occs,
                pages=pages_list,
                evidence_excerpt=occs[0].get("evidence_excerpt", "") if occs else "",
                source_models=sorted(grp["source_models"]),
                confidence=grp["confidence"],
                rationale=self._rationale(grp, occs),
            ))

        counts = {u: 0 for u in _REAL_UMBRELLAS}
        for c in candidates:
            counts[c["target_umbrella"]] = counts.get(c["target_umbrella"], 0) + 1

        hypothesis = ParserHypothesis(
            doc_id=doc_id,
            candidates=candidates,
            counts_by_umbrella=counts,
            dropped_count=dropped_count,
            dropped_examples=dropped_examples,
        )
        logger.info(
            "medical_ner: doc_id=%s deterministic post-process → %d candidate(s) "
            "(metadata=%d, variants=%d, biomarkers=%d, panel=%d) from %d raw, dropped=%d",
            doc_id, len(candidates),
            counts.get("report_metadata", 0),
            counts.get("Genomic_Variant_umbrella", 0),
            counts.get("other_molecular_biomarker_umbrella", 0),
            counts.get("tested_biomarker_umbrella", 0),
            len(raw_entities), dropped_count,
        )
        return hypothesis

    # -----------------------------------------------------------------------
    # Deterministic helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _empty_hypothesis(doc_id: str) -> ParserHypothesis:
        return ParserHypothesis(
            doc_id=doc_id,
            candidates=[],
            counts_by_umbrella={u: 0 for u in _REAL_UMBRELLAS},
            dropped_count=0,
            dropped_examples=[],
        )

    @staticmethod
    def _excerpt(page_text: str, start: int | None, end: int | None, *, pad: int = 50) -> str:
        if not page_text or start is None or end is None:
            return ""
        a = max(0, int(start) - pad)
        b = min(len(page_text), int(end) + pad)
        return re.sub(r"\s+", " ", page_text[a:b]).strip()

    def _field_hint_for(
        self,
        text: str,
        occurrences: list[Occurrence],
        text_by_page: dict[int, str],
    ) -> str | None:
        """Assign a report_metadata field via the YAML rules. Uses the FIRST
        occurrence whose surrounding block text matches a rule's near-label."""
        rules = self._mapping["field_hint_rules"]
        vendors = self._mapping["vendor_names"]

        # Vendor recognition by name match (label-agnostic).
        low = text.lower()
        if any(v in low for v in vendors):
            return "Vendor_Name"

        for occ in occurrences:
            page_text = text_by_page.get(occ.get("page") or 1, "")
            start = occ.get("char_start")
            if start is None:
                continue
            # Look at the ~80 chars to the left of the entity for a field label.
            left = page_text[max(0, int(start) - 80): int(start)].lower()
            # We don't have the entity's label on the occurrence; infer from
            # which rules could apply by checking near_labels against `left`.
            for rule in rules:
                near = [n.lower() for n in (rule.get("near_labels") or [])]
                if any(n in left for n in near):
                    return rule.get("field")
        return None

    @staticmethod
    def _rationale(grp: dict[str, Any], occurrences: list[Occurrence]) -> str:
        roles = sorted({o.get("text_role", "") for o in occurrences if o.get("text_role")})
        roles_str = ", ".join(roles) if roles else "unknown block"
        conf = "block-confirmed" if grp.get("block_confirmed") else "label-only fallback"
        return (
            f"{grp['text']!r} → {grp['target_umbrella']} "
            f"({conf}; {len(occurrences)} occurrence(s) in {roles_str})"
        )

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
        """End-to-end: spaCy extraction → deterministic mapper → ParserHypothesis."""
        raw = await self.extract_raw_entities(doc_id=doc_id, pages=pages)
        return await self.post_process(
            doc_id=doc_id,
            pages=pages,
            block_profiles=block_profiles,
            raw_entities=raw,
        )
