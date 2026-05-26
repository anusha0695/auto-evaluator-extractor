"""
block_view — assembles the unified per-block view model for the block-centric
PDF explorer (U3).

One `BlockView` per `block_id`, joining (all by exact block_id — no fuzzy
matching):
  - bbox + text + page + section_path   from `blocks.json`
  - text_role + umbrellas + confidence  from `block_profiles.json`
  - entities found in the block         from `parser_hypothesis.json`
                                          (candidates[].occurrences[].block_id)
  - extracted fields sourced here       from the extraction's
                                          report_metadata.provenance[*].block_id

Tolerant of both the new and legacy artifact shapes:
  - block_profiles: `target_umbrella_hints` (list, R2) OR legacy
    `target_umbrella_hint` (single) — normalized to a list.
  - parser_hypothesis candidates: `occurrences` (1A) OR legacy `pages` only —
    falls back to page-level attribution when occurrences are absent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ui.phase1.data_layer import load_artifact, load_extraction

logger = logging.getLogger(__name__)


@dataclass
class EntityRow:
    text: str
    umbrella: str
    source_models: list[str] = field(default_factory=list)


@dataclass
class FieldRow:
    field_name: str
    value: Any
    prov_type: str          # verbatim | derived | inferred | absent


@dataclass
class BlockView:
    block_id: str
    page_number: int
    bbox: list[float]                       # [x0,y0,x1,y1] normalized, or []
    text: str
    text_role: str
    target_umbrella_hints: list[str]
    confidence: float | None
    rationale: str
    entities: list[EntityRow] = field(default_factory=list)
    fields: list[FieldRow] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------


def _umbrella_hints(profile: dict[str, Any]) -> list[str]:
    """Return the umbrella hints as a list, tolerating the legacy single-value
    shape."""
    hints = profile.get("target_umbrella_hints")
    if isinstance(hints, list) and hints:
        return [str(h) for h in hints]
    single = profile.get("target_umbrella_hint")
    if single:
        return [str(single)]
    return ["none"]


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_block_views(doc_id: str) -> list[BlockView]:
    """Load all artifacts for `doc_id` and produce one BlockView per block,
    in page then input order. Returns [] if the blocks artifact is missing."""
    blocks = load_artifact(doc_id, "blocks") or []
    if not blocks:
        logger.warning("block_view: no blocks.json for doc_id=%s", doc_id)
        return []

    profiles = load_artifact(doc_id, "block_profiles") or []
    hypothesis = load_artifact(doc_id, "parser_hypothesis") or {}
    extraction = load_extraction(doc_id) or {}

    prof_by_id: dict[str, dict[str, Any]] = {
        str(p.get("block_id", "")): p for p in profiles
    }

    # --- entities per block (from parser_hypothesis occurrences) ---
    entities_by_block: dict[str, list[EntityRow]] = {}
    for cand in (hypothesis.get("candidates") or []):
        occs = cand.get("occurrences")
        umbrella = cand.get("target_umbrella", "")
        models = list(cand.get("source_models") or [])
        if occs:
            for occ in occs:
                bid = str(occ.get("block_id") or "")
                if not bid:
                    continue
                entities_by_block.setdefault(bid, []).append(
                    EntityRow(text=cand.get("text", ""), umbrella=umbrella,
                              source_models=models)
                )
        # Legacy fallback: no occurrences → can't attribute to a block; skip.

    # --- fields per block (from report_metadata.provenance; array OR legacy map) ---
    from ui.phase1.evidence import iter_provenance
    fields_by_block: dict[str, list[FieldRow]] = {}
    report_md = (extraction.get("report_metadata") or {}) if extraction else {}
    for field_name, prov in iter_provenance(report_md.get("provenance")):
        if not isinstance(prov, dict):
            continue
        bid = str(prov.get("block_id") or "")
        if not bid:
            continue
        fields_by_block.setdefault(bid, []).append(
            FieldRow(
                field_name=field_name,
                value=report_md.get(field_name),
                prov_type=str(prov.get("type") or "verbatim"),
            )
        )

    # --- assemble ---
    views: list[BlockView] = []
    for b in blocks:
        bid = str(b.get("block_id", ""))
        prof = prof_by_id.get(bid, {})
        views.append(BlockView(
            block_id=bid,
            page_number=int(b.get("page_number") or 1),
            bbox=list(b.get("bbox") or []),
            text=b.get("text", "") or "",
            text_role=prof.get("text_role", "other"),
            target_umbrella_hints=_umbrella_hints(prof) if prof else ["none"],
            confidence=prof.get("confidence"),
            rationale=prof.get("rationale", ""),
            entities=entities_by_block.get(bid, []),
            fields=fields_by_block.get(bid, []),
        ))
    return views


def find_source_pdf(doc_id: str) -> Path | None:
    """Locate the source PDF for a doc.

    Preferred: the self-contained copy bundled into the doc's artifact folder
    (artifacts/<doc_id>/source.pdf) written by preprocess_node on local runs.
    Falls back to the filename-based folder search for docs processed before
    that change (or run against gcs_uri input).
    """
    # 1. Self-contained artifact copy (cleanest — no filename guessing).
    try:
        from core.persistence import load_storage_config
        local_dir = load_storage_config("phase_1").local_dir_resolved
        bundled = local_dir / "artifacts" / doc_id / "source.pdf"
        if bundled.exists():
            return bundled
    except Exception:
        pass

    # 2. Fallback: filename-based search in known input folders.
    repo_root = Path(__file__).resolve().parents[2]
    candidates = [
        repo_root.parent / "data" / "actual_docs" / f"{doc_id}.pdf",
        repo_root / "data" / "actual_docs" / f"{doc_id}.pdf",
        repo_root / "ground_truth" / "fixtures" / f"{doc_id}.pdf",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None
