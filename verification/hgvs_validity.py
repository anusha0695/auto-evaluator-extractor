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


def hgvs_field_valid(leaf: str, value: str) -> bool:
    """True if `value` is structurally valid HGVS for field `leaf` — retrying with the
    field-appropriate prefix when the printed value omits it (legitimate per the schema)."""
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
    """Yield (ref, leaf, value) for every populated HGVS leaf anywhere in the envelope."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "occurrences":
                continue
            cref = f"{ref}.{k}"
            if isinstance(v, (dict, list)):
                yield from _walk(v, cref)
            elif k in FIELD_PREFIX and isinstance(v, str) and v.strip():
                yield cref, k, v
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, (dict, list)):
                yield from _walk(v, f"{ref}[{i}]")


def find_malformed_hgvs(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """Every populated HGVS leaf that is genuinely malformed (fails even after a
    prefix retry). Each entry routes to needs_review / escalate — NEVER renormalize
    (there is no canonical to write)."""
    out: list[dict[str, Any]] = []
    for sec, payload in (envelope or {}).items():
        if isinstance(payload, (dict, list)):
            for ref, leaf, value in _walk(payload, sec):
                if not hgvs_field_valid(leaf, value):
                    out.append({"ref": ref, "field_name": ref, "leaf": leaf,
                                "value": value, "status": "invalid_hgvs"})
    return out
