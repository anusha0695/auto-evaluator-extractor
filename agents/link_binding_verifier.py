"""
Link-&-Binding Verifier (Phase 2, M6) — a SEPARATE verifier from the Linker.

It never extracts or binds; it only confirms/refutes claims against the
re-fetched source, with evidence. Four checks (PHASE_2_DETAILED_DESIGN.md Step 4):

  V1  component grounding (DETERMINISTIC pre-check) — name/method/result each
      appear in a cited block. Fail → refuted, skip the LLM.
  V2  relationship confirm (LLM, INJECTED)          — does the span state
      {method}→{result} for {name}, not a neighbor? Catches mis-binds.
  V3  orphan / hallucination (DETERMINISTIC vs NER inventory) — every located
      result attached to a finding; every emitted result exists in source.
  V4  link confirm (LLM, INJECTED)                  — each cross-umbrella link
      supported by its evidence.

Cost discipline: a finding bound from a TABLE ROW (its occurrence cites a block
with a `table_id`) is trusted and SKIPS V2. Only narrative/inferred bindings
spend the LLM. In deterministic-only mode (no injected `relationship_confirm` /
`link_confirm`), narrative binds and links return `uncertain` → escalate
(VMAW/SME), never silently confirmed.

Verdict per item: confirmed | refuted | uncertain (+ evidence).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core.observability import trace as otel_trace

logger = logging.getLogger(__name__)


@dataclass
class Verdict:
    ref: str
    check: str                     # V1|V2|V3|V4
    verdict: str                   # confirmed|refuted|uncertain
    evidence: str = ""


@dataclass
class VerifyResult:
    verdicts: list[Verdict] = field(default_factory=list)
    @property
    def refuted(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.verdict == "refuted"]
    @property
    def uncertain(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.verdict == "uncertain"]
    @property
    def passed(self) -> bool:
        return not self.refuted and not self.uncertain


class LinkBindingVerifier:
    def __init__(
        self,
        *,
        relationship_confirm: Callable[..., dict] | None = None,   # V2 (LLM)
        link_confirm: Callable[..., dict] | None = None,           # V4 (LLM)
    ) -> None:
        self._relationship_confirm = relationship_confirm
        self._link_confirm = link_confirm

    @otel_trace("agents.link_binding_verifier.verify")
    def verify(
        self,
        *,
        envelope: dict[str, Any],
        links: list[Any],
        blocks: list[dict[str, Any]],
        parser_hypothesis: dict[str, Any] | None = None,
    ) -> VerifyResult:
        res = VerifyResult()
        text_by_id = {b.get("block_id"): (b.get("text") or "") for b in blocks}
        tableid_by_block = {b.get("block_id"): b.get("table_id") for b in blocks}

        biomarkers = (envelope.get("other_molecular_biomarker_umbrella") or {}).get(
            "other_molecular_biomarkers") or []

        # ----- V1 + V2 over biomarker findings -----
        for i, bm in enumerate(biomarkers):
            name = bm.get("biomarker_name") or ""
            for j, f in enumerate(bm.get("findings") or []):
                ref = f"other_molecular_biomarkers[{i}].findings[{j}]"
                result = f.get("result")
                occs = f.get("occurrences") or []
                # V1 grounding
                grounded = self._grounded(name, result, occs, text_by_id)
                if occs and not grounded:
                    res.verdicts.append(Verdict(ref, "V1", "refuted",
                        f"result '{result}' / name '{name}' not found in cited block(s)"))
                    continue
                res.verdicts.append(Verdict(ref, "V1", "confirmed" if grounded else "uncertain",
                    "components present in cited block" if grounded else "no occurrences to ground"))
                # V2 only for non-table-row binds
                is_table_bind = any(tableid_by_block.get(o.get("block_id")) for o in occs)
                if is_table_bind:
                    continue
                if self._relationship_confirm is not None:
                    try:
                        out = self._relationship_confirm(name=name, finding=f, blocks=blocks)
                        res.verdicts.append(Verdict(ref, "V2",
                            out.get("verdict", "uncertain"), out.get("evidence", "")))
                    except Exception as exc:  # noqa: BLE001
                        res.verdicts.append(Verdict(ref, "V2", "uncertain", f"V2 error: {exc}"))
                else:
                    res.verdicts.append(Verdict(ref, "V2", "uncertain",
                        "narrative bind; no LLM relationship-confirm → escalate"))

        # ----- V3 orphan / hallucination (deterministic) -----
        res.verdicts.extend(self._v3_orphan_hallucination(envelope, text_by_id, parser_hypothesis))

        # ----- V4 link confirm -----
        for k, lk in enumerate(links):
            method = getattr(lk, "method", None) if not isinstance(lk, dict) else lk.get("method")
            ref = f"links[{k}]"
            if method == "deterministic":
                res.verdicts.append(Verdict(ref, "V4", "confirmed", "deterministic gene-key link"))
                continue
            if self._link_confirm is not None:
                try:
                    out = self._link_confirm(link=lk, envelope=envelope, blocks=blocks)
                    res.verdicts.append(Verdict(ref, "V4", out.get("verdict", "uncertain"),
                                                out.get("evidence", "")))
                except Exception as exc:  # noqa: BLE001
                    res.verdicts.append(Verdict(ref, "V4", "uncertain", f"V4 error: {exc}"))
            else:
                res.verdicts.append(Verdict(ref, "V4", "uncertain",
                    "contextual link; no LLM link-confirm → escalate"))
        return res

    # -----------------------------------------------------------------------

    @staticmethod
    def _grounded(name: str, result: Any, occurrences: list[dict], text_by_id: dict) -> bool:
        if not occurrences:
            return False
        needle_result = (str(result).lower() if result is not None else None)
        for o in occurrences:
            txt = (text_by_id.get(o.get("block_id")) or "").lower()
            if not txt:
                continue
            surface = (o.get("surface") or "").lower()
            ok_name = (not name) or (name.lower() in txt) or (name.lower() in surface)
            ok_result = (needle_result is None) or (needle_result in txt) or (needle_result in surface)
            if ok_name and ok_result:
                return True
        return False

    @staticmethod
    def _emitted_haystack(biomarkers: list[dict[str, Any]]) -> str:
        """ALL strings the biomarker records actually emitted — names, results,
        interpretations, every variant_detail value, and occurrence surfaces. A
        candidate whose text appears anywhere here IS attached (so it is NOT an
        orphan). The previous check compared against biomarker_name ONLY, which
        false-flagged every variant specific (V617F, c.1849G>T) and every
        non-biomarker NER token as an 'orphan' → SME flood."""
        bits: list[str] = []

        def _harvest(o: Any) -> None:
            if isinstance(o, dict):
                for v in o.values():
                    _harvest(v)
            elif isinstance(o, list):
                for v in o:
                    _harvest(v)
            elif isinstance(o, (str, int, float)) and not isinstance(o, bool):
                bits.append(str(o))

        _harvest(biomarkers)
        return " ".join(bits).lower()

    @staticmethod
    def _illegible(text: str) -> bool:
        """Cheap legibility heuristic: is this source text genuinely jumbled/garbled
        (so a human is needed to even read it), vs. a clean token that simply isn't a
        biomarker? Clean tokens (MRN, dates, '1849G>', 'V617F') return False → drop;
        OCR-splatter ('J@K2 V6l7F~~|') returns True → escalate."""
        t = (text or "").strip()
        if len(t) < 3:
            return False                       # too short to call 'jumbled' — that's absence, not garble
        # "Jumbled" = OCR symbol-splatter, NOT a clean numeric token. Dates
        # (08/12/2025), MRNs, accession #s and gene codes (1849G>) are perfectly
        # legible despite having few/no letters, so a letter-ratio test would wrongly
        # escalate them. The genuine signal is a high share of printable noise that
        # isn't alphanumeric, whitespace, or ordinary clinical punctuation.
        noise = sum(1 for c in t if not c.isalnum() and not c.isspace()
                    and c not in ".,:;%/()+-<>=*'\"#")
        return (noise / len(t)) > 0.30

    @classmethod
    def _v3_orphan_hallucination(
        cls, envelope: dict[str, Any], text_by_id: dict, parser_hypothesis: dict[str, Any] | None,
    ) -> list[Verdict]:
        """V3 — attachment / hallucination, generic over every section a candidate can route to.

        For each NER candidate routed to umbrella U, the haystack is built from EVERY
        section the candidate's NER label is allowed to route to (per config/ner_mapping.yaml).
        For v4 a gene candidate is routed to Genomic_Variant_umbrella + tested + biomarker,
        so a string captured verbatim in any of those three counts as attached. v3 envelopes
        only carry biomarker + tested → behaviour is unchanged. The hallucination check
        (emitted name not in source) still scopes to the biomarker section it ran against."""
        out: list[Verdict] = []
        all_text = " ".join(text_by_id.values()).lower()

        # Hallucination check — biomarker section only (variant section is HGVS-validated separately).
        biomarkers = (envelope.get("other_molecular_biomarker_umbrella") or {}).get(
            "other_molecular_biomarkers") or []
        for i, bm in enumerate(biomarkers):
            name = (bm.get("biomarker_name") or "").lower()
            if name and all_text and name not in all_text:
                out.append(Verdict(f"other_molecular_biomarkers[{i}]", "V3", "refuted",
                                   f"biomarker '{name}' not present in any source block (possible hallucination)"))

        if not parser_hypothesis:
            return out

        # Build a per-umbrella haystack ONCE (used as fallback when a candidate has no label).
        haystack_by_section: dict[str, str] = {}
        for section_name, payload in (envelope or {}).items():
            haystack_by_section[section_name] = cls._emitted_haystack_any(payload)

        # Read the NER label→umbrellas table from config so the orphan haystack and
        # routing share one source of truth.
        label_routes = cls._ner_label_routes()

        for c in (parser_hypothesis.get("candidates") or []):
            target = c.get("target_umbrella")
            label = c.get("entity_label") or c.get("label")
            t = (c.get("text") or "")
            tl = t.lower()
            if not tl:
                continue
            # the set of sections this candidate could legitimately have landed in.
            # Prefer label→umbrellas (config); fall back to the single target_umbrella;
            # final fallback to every section in the envelope.
            sections = list(label_routes.get(str(label or ""), [])) or (
                [target] if target else list(haystack_by_section))
            # attached if its text appears in ANY of those sections' emitted strings.
            attached = any(tl in (haystack_by_section.get(s) or "") for s in sections)
            if attached:
                continue
            # Only THIS-umbrella candidates can become V3 verdicts (we don't speak for
            # other sections' own verifiers).
            if target != "other_molecular_biomarker_umbrella":
                continue
            src = [text_by_id.get(o.get("block_id")) or ""
                   for o in (c.get("occurrences") or [])]
            garbled = cls._illegible(t) or any(cls._illegible(s) for s in src)
            flagged = bool(c.get("needs_review"))
            if garbled or flagged:
                out.append(Verdict(f"candidate:{t}", "V3", "uncertain",
                    "unattached biomarker candidate over illegible/garbled source — "
                    "human read needed" if garbled else
                    "unattached biomarker candidate flagged needs_review"))
            else:
                logger.info("V3 orphan dropped (clean, not present in any routable section, not escalated): %r [routes=%s]", t, sections)
        return out

    @staticmethod
    def _emitted_haystack_any(payload: Any) -> str:
        """Lowercased concatenation of every string/number/bool-free leaf anywhere inside
        `payload`. Used to test attachment for an arbitrary section."""
        bits: list[str] = []
        def _walk(o: Any) -> None:
            if isinstance(o, dict):
                for v in o.values():
                    _walk(v)
            elif isinstance(o, list):
                for v in o:
                    _walk(v)
            elif isinstance(o, (str, int, float)) and not isinstance(o, bool):
                bits.append(str(o))
        _walk(payload)
        return " ".join(bits).lower()

    @staticmethod
    def _ner_label_routes() -> dict[str, list[str]]:
        """Read config/ner_mapping.yaml:label_to_umbrellas once. Returns {} on failure."""
        import yaml
        from pathlib import Path
        try:
            raw = yaml.safe_load(Path("config/ner_mapping.yaml").read_text(encoding="utf-8")) or {}
            m = raw.get("label_to_umbrellas") or {}
            return {str(k): list(v or []) for k, v in m.items()}
        except Exception:  # noqa: BLE001
            return {}
