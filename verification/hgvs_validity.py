"""
HGVS structural-validity floor (v4) — deterministic, offline.

The NormalizationVerifier already flags HGVS fields whose CANONICAL differs from the
verbatim (→ renormalize). This module covers the OTHER half: a printed HGVS change that
is genuinely MALFORMED (OCR-garbled) and has no canonical at all — which has no business
being silently kept. Malformed HGVS has no canonical, so it must route to review /
needs_review, NOT renormalize (the routing wiring lands when the v4 graph is assembled —
V4-M5/M6; this module is the detector + its gate-tested logic).

Key nuance: the new schema emits change notation VERBATIM "even without the c./p./g.
prefix" (e.g. `V617F`, `1849G>T`). Those are LEGITIMATE — not malformed. So a value is
flagged ONLY if it fails `hgvs_validate` even after retrying with the field-appropriate
prefix. `V617F` in `amino_acid_change` → retry `p.V617F` → valid → NOT flagged;
`c.18_garbled!!` → already prefixed, still fails → flagged.

Pure / offline (regex backend in the sandbox; biocommons when installed).
"""

from __future__ import annotations

import re
from typing import Any

# leaf field name → the HGVS kind/prefix it should carry
FIELD_PREFIX = {
    "coding_dna_change": "c",
    "genomic_dna_change": "g",
    "amino_acid_change": "p",
}

_HAS_PREFIX = re.compile(r"^\s*(?:[A-Za-z0-9_.()-]+:)?[cgmnrp]\.", re.IGNORECASE)


def hgvs_field_valid(leaf: str, value: str, record: dict[str, Any] | None = None) -> bool:
    """True if `value` is structurally valid HGVS for field `leaf`.

    Order of trust (HGVS-OPTIONS Option 1 — user's locked decision):
      1. If `record.hgvs_normalized.valid` is True (the extractor already called
         `hgvs_validate` during extraction and the tool said valid), accept it.
      2. If the agent CITED the value (`record.evidence_block_ids` or
         `record.occurrences[].block_id` present), trust it — the agent read the
         source, decided it's HGVS, and pointed at the block. A byte-level
         re-validation with a dumber regex tool only re-rejects OCR artifacts
         (unicode minus `U+2212`, hyphen `U+2010`, NBSP, etc.) that the agent
         already saw the right glyph for. The schema asks for the value VERBATIM
         anyway, and the binding verifier (V1) is the dedicated grounding check.
      3. Otherwise — UNCITED — run the deterministic check (regex / biocommons):
            (a) as-is, then (b) prefix-retry with `c.`/`g.`/`p.`. This is the
         hallucination safety net for values the agent didn't ground.

    Net effect: grounded HGVS NEVER lands on the invalid_hgvs path → never
    reaches VMAW → never reaches SME for the OCR-artifact class the user
    explicitly does not want escalated.
    """
    # (1) Trust the agent's own tool result when present.
    norm = (record or {}).get("hgvs_normalized") if isinstance(record, dict) else None
    if isinstance(norm, dict) and norm.get("valid") is True:
        return True

    # (2) Trust grounded extractions (HGVS-OPTIONS Option 1).
    if isinstance(record, dict):
        if record.get("evidence_block_ids"):
            return True
        occs = record.get("occurrences")
        if isinstance(occs, list) and any(
            isinstance(o, dict) and o.get("block_id") for o in occs
        ):
            return True

    # (3) Uncited values: run the deterministic safety-net check.
    from preprocess.hgvs_validate import hgvs_validate
    raw = (value or "").strip()
    if not raw:
        return True  # empty/null is not 'malformed' — absence, handled elsewhere
    if hgvs_validate(raw).get("valid"):
        return True
    pref = FIELD_PREFIX.get(leaf)
    if pref and not _HAS_PREFIX.match(raw):
        return bool(hgvs_validate(f"{pref}.{raw}").get("valid"))
    return False


def _walk(obj: Any, ref: str):
    """Yield (ref, leaf, value, record) for every populated HGVS leaf anywhere in
    the envelope. `record` is the IMMEDIATE parent dict of the leaf — so
    `record["hgvs_normalized"]` (if present) is the sibling the agent stored its
    own validation result in."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "occurrences":
                continue
            cref = f"{ref}.{k}"
            if isinstance(v, (dict, list)):
                yield from _walk(v, cref)
            elif k in FIELD_PREFIX and isinstance(v, str) and v.strip():
                yield cref, k, v, obj
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, (dict, list)):
                yield from _walk(v, f"{ref}[{i}]")


def find_malformed_hgvs(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """Every populated HGVS leaf that is genuinely malformed (fails even after a
    prefix retry AND the agent's own hgvs_normalized.valid is not True). Each
    entry routes to needs_review / escalate — NEVER renormalize (there is no
    canonical to write)."""
    out: list[dict[str, Any]] = []
    for sec, payload in (envelope or {}).items():
        if isinstance(payload, (dict, list)):
            for ref, leaf, value, record in _walk(payload, sec):
                if not hgvs_field_valid(leaf, value, record=record):
                    out.append({"ref": ref, "field_name": ref, "leaf": leaf,
                                "value": value, "status": "invalid_hgvs"})
    return out
