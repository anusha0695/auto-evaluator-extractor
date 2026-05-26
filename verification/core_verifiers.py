"""
Phase 2 deterministic verifiers (M6). Each returns a VerifierScorecard-shaped
dict {verifier_name, passed, field_errors, notes}.

  - CoverageVerifier      ("coverage_audit")    : envelope vs parser-hypothesis
        candidates per umbrella → misses (a located entity absent from output).
  - LinkConsistencyVerifier ("link_consistency") : every Link ref resolves to a
        real entry on both sides; no duplicate links.
  - EvidenceConfidenceVerifier ("evidence_confidence") : every populated
        variant/biomarker record cites provenance (occurrences[]); flags
        ungrounded records and low section confidence.

The separate LLM relationship verifier is agents/link_binding_verifier.py.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def _scorecard(name: str, passed: bool, errors: list[dict[str, Any]], notes: str) -> dict[str, Any]:
    return {"verifier_name": name, "passed": passed, "field_errors": errors[:25], "notes": notes}


# ---------------------------------------------------------------------------
# Coverage verifier
# ---------------------------------------------------------------------------


class CoverageVerifier:
    """Output vs parser-hypothesis. A candidate routed to an umbrella whose text
    never appears in that umbrella's emitted payload is a *potential miss*.

    ADVISORY ONLY (`passed` is always True): the SciSpaCy parser hypothesis is a
    noisy seed (raw candidate strings rarely appear verbatim in structured
    output), and the authoritative recall gate is the per-team Coverage Auditor
    (LLM) that already ran inside each team. This envelope-level check surfaces a
    miss ratio for review/UI but must not hard-fail the verdict — otherwise
    almost every doc sme-flags on NER noise. Set `hard_gate=True` to restore
    gating once the parser hypothesis is high-precision."""

    def __init__(self, *, gap_tolerance: float = 0.10, hard_gate: bool = False) -> None:
        self._tol = gap_tolerance
        self._hard_gate = hard_gate

    def verify(self, envelope: dict[str, Any], parser_hypothesis: dict[str, Any]) -> dict[str, Any]:
        candidates = (parser_hypothesis or {}).get("candidates") or []
        errors: list[dict[str, Any]] = []
        considered = 0
        for c in candidates:
            section = c.get("target_umbrella")
            text = (c.get("text") or "").strip()
            if not section or section == "none" or not text:
                continue
            payload = envelope.get(section)
            if payload is None:
                continue
            considered += 1
            hay = json.dumps(payload, default=str).lower()
            if text.lower() not in hay:
                errors.append({
                    "section": section, "loc": ["<coverage>"],
                    "msg": f"located entity '{text}' not found in {section} output",
                })
        miss_ratio = (len(errors) / considered) if considered else 0.0
        within_tol = miss_ratio <= self._tol
        passed = within_tol if self._hard_gate else True
        prefix = "" if self._hard_gate else "ADVISORY "
        return _scorecard(
            "coverage_audit", passed, errors,
            f"{prefix}{len(errors)} potential miss(es) over {considered} candidate(s); "
            f"ratio={miss_ratio:.2f} tol={self._tol}"
            + ("" if within_tol or not self._hard_gate else " — exceeds tolerance"),
        )


# ---------------------------------------------------------------------------
# Link consistency verifier
# ---------------------------------------------------------------------------


_REF_RE = re.compile(r"^(?P<section>[A-Za-z_]+)\.(?P<arr>[A-Za-z_]+)\[(?P<idx>\d+)\]$")


class LinkConsistencyVerifier:
    def verify(self, envelope: dict[str, Any], links: list[Any]) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        seen: set[tuple] = set()
        for lk in links:
            frm = getattr(lk, "from_ref", None) if not isinstance(lk, dict) else lk.get("from_ref")
            to = getattr(lk, "to_ref", None) if not isinstance(lk, dict) else lk.get("to_ref")
            typ = getattr(lk, "type", None) if not isinstance(lk, dict) else lk.get("type")
            for ref in (frm, to):
                if not self._resolves(envelope, ref):
                    errors.append({"section": "<link>", "loc": [ref or ""],
                                   "msg": f"dangling link ref '{ref}'"})
            key = (frm, to, typ)
            if key in seen:
                errors.append({"section": "<link>", "loc": [str(key)], "msg": "duplicate link"})
            seen.add(key)
        return _scorecard("link_consistency", not errors, errors,
                          f"{len(links)} link(s), {len(errors)} issue(s)")

    @staticmethod
    def _resolves(envelope: dict[str, Any], ref: str | None) -> bool:
        if not ref:
            return False
        m = _REF_RE.match(ref)
        if not m:
            return False
        section = envelope.get(m.group("section"))
        if not isinstance(section, dict):
            return False
        arr = section.get(m.group("arr"))
        if not isinstance(arr, list):
            return False
        return 0 <= int(m.group("idx")) < len(arr)


# ---------------------------------------------------------------------------
# Evidence / confidence verifier
# ---------------------------------------------------------------------------


class EvidenceConfidenceVerifier:
    """Every populated variant/biomarker record must cite provenance
    (occurrences[]). Flags ungrounded records + low section confidence."""

    def __init__(self, *, min_confidence: float = 0.70) -> None:
        self._min_conf = min_confidence

    def verify(self, envelope: dict[str, Any]) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []

        # v3 merge: sequence variants are biomarker findings (with a
        # variant_detail), so the single biomarker-findings provenance check
        # below covers them too.
        ob = (envelope.get("other_molecular_biomarker_umbrella") or {})
        for i, bm in enumerate(ob.get("other_molecular_biomarkers") or []):
            for j, f in enumerate(bm.get("findings") or []):
                if (f.get("result") is not None) and not f.get("occurrences"):
                    errors.append({"section": "other_molecular_biomarker_umbrella",
                                   "loc": [f"other_molecular_biomarkers[{i}].findings[{j}]"],
                                   "msg": "finding with a result has no occurrences[] provenance"})

        # low-confidence note (not a hard fail by itself).
        low_conf: list[str] = []
        for sec in ("other_molecular_biomarker_umbrella",
                    "tested_biomarker_umbrella", "clinical_information"):
            payload = envelope.get(sec) or {}
            conf = payload.get("llm_confidence_score")
            if isinstance(conf, (int, float)) and conf < self._min_conf:
                low_conf.append(f"{sec}={conf}")

        notes = f"{len(errors)} ungrounded record(s)"
        if low_conf:
            notes += f"; low confidence: {', '.join(low_conf)}"
        return _scorecard("evidence_confidence", not errors, errors, notes)

    @staticmethod
    def _is_populated(rec: dict[str, Any]) -> bool:
        skip = {"page_number", "occurrences", "needs_review", "review_reason",
                "specimen_id", "assertion"}
        return any(v not in (None, "", []) for k, v in rec.items() if k not in skip)
