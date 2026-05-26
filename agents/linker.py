"""
Linker — the unified contextual capability (Phase 2, M5).

One module, three operations + assembly, run AFTER all teams:

  - ASSEMBLE  : wrap every team's section output into the full schema-v3
                envelope (deterministic).
  - LINK      : cross-section relationships. Deterministic gene-key seed
                (variant `gene_studied` ↔ `tested_biomarkers` via HGNC); the
                contextual links (finding ↔ interpretation, biomarker ↔ specimen)
                come from an INJECTED LLM adjudicator.
  - GROUP/dedup-vs-group : deterministic normalized-key SEED only; the ambiguous
                MERGE-vs-KEEP_SEPARATE decision comes from the injected LLM
                adjudicator. (Within-section findings[] grouping is already done
                by each team's Extractor per its prompt.)
  - SUPERSESSION : deterministic detection on `addendum`-tagged blocks. When the
                addendum clearly names (biomarker, method, result) the amended
                value wins and the original is kept with `superseded:true`;
                otherwise the finding is flagged `needs_review` for VMAW/SME.

LLM operations are dependency-injected (`merge_adjudicator`, `link_adjudicator`,
`supersession_resolver`). In deterministic-only mode (no adjudicators — e.g. the
M5 gate) ambiguous cases ESCALATE (flagged `needs_review`) rather than guess.
graph_linear (M8) injects the real Gemini-backed adjudicators. Every escalation
point is catalogued in PHASE_2_VMAW_TRIGGERS.md.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core.observability import trace as otel_trace

logger = logging.getLogger(__name__)

# section name → (array_holder_key, item_list_key) for the array umbrellas.
# v3 merge: the former Genomic_Variant_umbrella folded into the biomarker
# umbrella, so gene variants are biomarker entries carrying a `variant_detail`.
_BIOMARKER_SECTION = "other_molecular_biomarker_umbrella"
_TESTED_SECTION = "tested_biomarker_umbrella"


@dataclass
class Link:
    from_ref: str
    to_ref: str
    type: str                         # e.g. "variant_on_panel"
    rationale: str
    evidence_block_ids: list[str] = field(default_factory=list)
    confidence: float = 1.0
    method: str = "deterministic"     # "deterministic" | "contextual"


@dataclass
class LinkResult:
    envelope: dict[str, Any]
    links: list[Link] = field(default_factory=list)
    needs_review_refs: list[dict[str, str]] = field(default_factory=list)  # {ref, reason}
    notes: str = ""


class Linker:
    def __init__(
        self,
        *,
        hgnc_normalize: Callable[[str], dict] | None = None,
        merge_adjudicator: Callable[..., dict] | None = None,
        link_adjudicator: Callable[..., list] | None = None,
        supersession_resolver: Callable[..., dict] | None = None,
        link_registry: Any | None = None,
        min_link_confidence: float = 0.5,
        link_seed_enabled: bool = True,
    ) -> None:
        # default HGNC normalizer (the real tool) unless one is injected (tests).
        if hgnc_normalize is None:
            from preprocess.hgnc_resolver import hgnc_normalize as _h
            hgnc_normalize = _h
        self._hgnc = hgnc_normalize
        self._merge_adjudicator = merge_adjudicator
        self._link_adjudicator = link_adjudicator
        self._supersession_resolver = supersession_resolver
        # P3-M5 contextual-linking config.
        self._registry = link_registry          # LinkRegistry | None (None → legacy validation)
        self._min_link_conf = float(min_link_confidence)
        # Deterministic gene-key seed: a CONFIG TOGGLE (default on, measured).
        # When True the gene-key links are committed (legacy behavior). When
        # False they are passed to the adjudicator as non-binding HINTS only and
        # the agent makes the final call (pure-contextual A/B arm).
        self._link_seed_enabled = bool(link_seed_enabled)

    # -----------------------------------------------------------------------
    # Public
    # -----------------------------------------------------------------------

    @otel_trace("agents.linker.link")
    def link(
        self,
        *,
        sections: dict[str, dict[str, Any]],
        blocks: list[dict[str, Any]] | None = None,
        block_profiles: list[dict[str, Any]] | None = None,
        relink_hints: list[dict[str, Any]] | None = None,
    ) -> LinkResult:
        """Assemble + link the section outputs. `sections` maps schema section
        name → that team's emitted payload. `blocks`/`block_profiles` power
        supersession (addendum detection)."""
        envelope = self._assemble_envelope(sections)
        result = LinkResult(envelope=envelope)

        # LINK (deterministic gene-key seed). P3-M5: the seed is a CONFIG TOGGLE.
        #  - seed ON  → commit the gene-key links (legacy behavior) AND pass them
        #               to the adjudicator as hints.
        #  - seed OFF → DON'T commit; pass them as non-binding hints only and let
        #               the contextual agent decide (pure-contextual A/B arm).
        seed_links = self._gene_key_links(envelope)
        if self._link_seed_enabled:
            result.links.extend(seed_links)
        seed_hints = [
            {"from_ref": l.from_ref, "to_ref": l.to_ref, "type": l.type, "rationale": l.rationale}
            for l in seed_links
        ]

        # LINK (contextual, injected) — the PRIMARY link producer (P3-M5). Fed the
        # typed-link catalog + seed hints; emits typed links. The LLM may propose
        # links with refs it invented or types it shouldn't, so VALIDATE every one
        # deterministically (type in registry + endpoints match + refs resolve +
        # evidence cited + confidence floor) and DROP failures — a bad link is the
        # adjudicator's error, never ground truth. The verifier re-checks too.
        if self._link_adjudicator is not None:
            try:
                existing = {(l.from_ref, l.to_ref, l.type) for l in result.links}
                proposed = self._call_link_adjudicator(envelope, blocks, seed_hints, relink_hints) or []
                kept = dropped = 0
                for lk in proposed:
                    frm, to, typ = lk.get("from_ref"), lk.get("to_ref"), lk.get("type")
                    ok, why = self._validate_contextual_link(lk, envelope, blocks)
                    if not ok:
                        dropped += 1
                        logger.debug("Linker: dropped contextual link (%s): %s", why, lk)
                        continue
                    if (frm, to, typ) in existing:
                        dropped += 1
                        continue
                    existing.add((frm, to, typ))
                    result.links.append(Link(method="contextual", **lk))
                    kept += 1
                if proposed:
                    logger.info("Linker: contextual links kept=%d dropped(invalid/dupe)=%d", kept, dropped)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Linker: link_adjudicator failed: %s", exc)

        # SUPERSESSION (deterministic detection on addendum blocks)
        self._apply_supersession(envelope, blocks or [], block_profiles or [], result)

        # count_of_extracted_objects
        envelope["count_of_extracted_objects"] = self._count_objects(envelope)
        return result

    # -----------------------------------------------------------------------
    # Contextual-link plumbing (P3-M5)
    # -----------------------------------------------------------------------

    def _call_link_adjudicator(
        self,
        envelope: dict[str, Any],
        blocks: list[dict[str, Any]] | None,
        seed_hints: list[dict[str, Any]],
        relink_hints: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Call the injected adjudicator, feeding the typed-link catalog + seed hints
        + (gap #3) `avoid_hints` = refuted pairings the binding verifier rejected, so a
        re-link RE-EVALUATES them instead of blindly re-emitting. Three-tier fallback
        keeps older/stub adjudicators working: avoid_hints → M5 kwargs → (envelope, blocks)."""
        catalog = self._registry.catalog_text() if self._registry is not None else None
        try:
            return self._link_adjudicator(
                envelope=envelope, blocks=blocks, link_catalog=catalog,
                seed_hints=seed_hints, avoid_hints=relink_hints or [],
            )
        except TypeError:
            pass
        try:
            return self._link_adjudicator(
                envelope=envelope, blocks=blocks, link_catalog=catalog, seed_hints=seed_hints,
            )
        except TypeError:
            return self._link_adjudicator(envelope=envelope, blocks=blocks)

    def _validate_contextual_link(
        self,
        link: dict[str, Any],
        envelope: dict[str, Any],
        blocks: list[dict[str, Any]] | None,
    ) -> tuple[bool, str]:
        """Deterministic safety net for a proposed contextual link. Uses the
        typed registry when present (type + endpoints + evidence + confidence);
        otherwise falls back to the legacy resolve-both-refs check."""
        if self._registry is not None:
            return self._registry.validate_link(
                link=link, envelope=envelope, blocks=blocks,
                min_confidence=self._min_link_conf,
            )
        # Legacy (no registry): just require both refs to resolve.
        if self._ref_resolves(envelope, link.get("from_ref")) and \
           self._ref_resolves(envelope, link.get("to_ref")):
            return True, "ok (legacy)"
        return False, "dangling ref (legacy)"

    # -----------------------------------------------------------------------
    # ASSEMBLE
    # -----------------------------------------------------------------------

    _REF_RE = re.compile(r"^(?P<section>[A-Za-z_]+)\.(?P<arr>[A-Za-z_]+)\[(?P<idx>\d+)\]$")

    @classmethod
    def _ref_resolves(cls, envelope: dict[str, Any], ref: str | None) -> bool:
        """True iff ref like 'Section.array[idx]' points at a real envelope item."""
        if not ref:
            return False
        m = cls._REF_RE.match(ref)
        if not m:
            return False
        section = envelope.get(m.group("section"))
        if not isinstance(section, dict):
            return False
        arr = section.get(m.group("arr"))
        if not isinstance(arr, list):
            return False
        return 0 <= int(m.group("idx")) < len(arr)

    @staticmethod
    def _assemble_envelope(sections: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Wrap section payloads into the full v3 envelope, filling absent
        sections with empty placeholders."""
        env: dict[str, Any] = {
            "count_of_extracted_objects": 0,
            "report_metadata": sections.get("report_metadata"),
            "other_molecular_biomarker_umbrella": sections.get(_BIOMARKER_SECTION)
                or {"count": 0, "llm_confidence_score": None, "other_molecular_biomarkers": []},
            "tested_biomarker_umbrella": sections.get(_TESTED_SECTION)
                or {"count_of_tested_biomarkers": 0, "page_numbers": [], "llm_confidence_score": None, "tested_biomarkers": []},
            "significant_findings": sections.get("significant_findings")
                or {"count_of_specimen_findings": 0, "llm_confidence_score": None, "specimen_findings": []},
            "clinical_information": sections.get("clinical_information"),
        }
        return env

    @staticmethod
    def _count_objects(env: dict[str, Any]) -> int:
        n = 0
        n += len((env.get("other_molecular_biomarker_umbrella") or {}).get("other_molecular_biomarkers") or [])
        n += len((env.get("tested_biomarker_umbrella") or {}).get("tested_biomarkers") or [])
        n += len((env.get("significant_findings") or {}).get("specimen_findings") or [])
        return n

    # -----------------------------------------------------------------------
    # LINK — deterministic gene-key
    # -----------------------------------------------------------------------

    def _canon(self, symbol: str | None) -> str | None:
        if not symbol:
            return None
        r = self._hgnc(symbol)
        return r.get("canonical")

    def _gene_key_links(self, env: dict[str, Any]) -> list[Link]:
        """Link each biomarker that is a gene sequence variant (carries a
        `variant_detail`) to the panel entry for the same HGNC-normalized gene
        (variant_on_panel). v3 merge: variants are biomarker entries now."""
        links: list[Link] = []
        biomarkers = (env.get(_BIOMARKER_SECTION) or {}).get("other_molecular_biomarkers") or []
        panel = (env.get("tested_biomarker_umbrella") or {}).get("tested_biomarkers") or []
        panel_canon = [(j, self._canon(p)) for j, p in enumerate(panel)]
        for i, b in enumerate(biomarkers):
            # only variants (a finding with a non-null variant_detail) link as variant_on_panel
            has_variant = any(
                isinstance(f, dict) and f.get("variant_detail")
                for f in (b.get("findings") or [])
            )
            if not has_variant:
                continue
            gv = self._canon(b.get("biomarker_name"))
            if not gv:
                continue
            for j, pc in panel_canon:
                if pc and pc == gv:
                    links.append(Link(
                        from_ref=f"{_BIOMARKER_SECTION}.other_molecular_biomarkers[{i}]",
                        to_ref=f"{_TESTED_SECTION}.tested_biomarkers[{j}]",
                        type="variant_on_panel",
                        rationale=f"Same HGNC-normalized gene '{gv}'.",
                        confidence=1.0, method="deterministic",
                    ))
        return links

    # -----------------------------------------------------------------------
    # SUPERSESSION — deterministic detection on addendum blocks
    # -----------------------------------------------------------------------

    def _apply_supersession(
        self,
        env: dict[str, Any],
        blocks: list[dict[str, Any]],
        block_profiles: list[dict[str, Any]],
        result: LinkResult,
    ) -> None:
        """For each `addendum`-tagged block, find biomarker findings whose
        (name, method) the addendum text mentions and reconcile them.

        Deterministic path: when a `supersession_resolver` is injected and can
        parse the amended (name, method, result), the amended value wins
        (original kept `superseded:true`). Otherwise we DETECT + FLAG the finding
        `needs_review` citing the addendum block — never silently rewrite."""
        addendum_ids = {
            bp.get("block_id") for bp in block_profiles
            if bp.get("text_role") == "addendum"
        }
        if not addendum_ids:
            return
        text_by_id = {b.get("block_id"): (b.get("text") or "") for b in blocks}
        addendum_texts = {bid: text_by_id.get(bid, "") for bid in addendum_ids}

        biomarkers = (env.get("other_molecular_biomarker_umbrella") or {}).get("other_molecular_biomarkers") or []
        for bi, bm in enumerate(biomarkers):
            name = (bm.get("biomarker_name") or "")
            if not name:
                continue
            for bid, atext in addendum_texts.items():
                if name.lower() in atext.lower():
                    ref = f"other_molecular_biomarker_umbrella.other_molecular_biomarkers[{bi}]"
                    resolved = None
                    if self._supersession_resolver is not None:
                        try:
                            resolved = self._supersession_resolver(
                                biomarker=bm, addendum_text=atext, addendum_block_id=bid)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("Linker: supersession_resolver failed: %s", exc)
                    if resolved and resolved.get("applied"):
                        # Resolver applied the override in-place (mutated bm) and
                        # marked the superseded occurrence; nothing else to do.
                        continue
                    # Detection-only → flag for VMAW/SME.
                    bm["needs_review"] = True
                    bm["review_reason"] = (
                        f"Addendum block {bid} mentions '{name}' — possible amended "
                        f"result; requires verification (supersession)."
                    )
                    result.needs_review_refs.append(
                        {"ref": ref, "reason": f"addendum {bid} may supersede a finding"})
                    break
