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

    Order of trust:
      1. If `record.hgvs_normalized.valid` is True (the extractor already called
         `hgvs_validate` during extraction and the tool said valid), accept it —
         no re-check. This avoids re-running the same validator on the same value
         and dodging a downstream invalid_hgvs → VMAW → SME round-trip when the
         agent already did the work.
      2. Otherwise, run `hgvs_validate` against the value as-is.
      3. If that fails AND the printed value lacks the field's prefix (c./g./p.),
         retry once with the prefix. Per the v4 schema, "1849G>T" verbatim is
         legitimate; the verifier just needs to confirm it parses as c.1849G>T.

    Only when ALL of the above fail is the value considered genuinely malformed.
    """
    # (1) Trust the agent's own tool result when present.
    norm = (record or {}).get("hgvs_normalized") if isinstance(record, dict) else None
    if isinstance(norm, dict) and norm.get("valid") is True:
        return True

    from preprocess.hgvs_validate import hgvs_validate
    raw = (value or "").strip()
    if not raw:
        return True  # empty/null is not 'malformed' — absence, handled elsewhere
    # (2) try as-is
    if hgvs_validate(raw).get("valid"):
        return True
    # (3) prefix retry
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
