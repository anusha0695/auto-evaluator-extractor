"""
NormalizationVerifier (gap #2) — deterministic, offline.

Walks the envelope for the leaf fields in `config/normalizer_map.yaml`, runs the
matching deterministic normalizer, and flags a field as `renormalizable` when the
normalizer matched a canonical that DIFFERS from the extracted value (e.g.
"c-Met" → "MET", "p.V600E" → normalized HGVS). The triage layer turns these into
`normalization_invalid` defects → `renormalize_field` (write canonical, keep the
verbatim string in the record's occurrences). No LLM — builds the normalizers
itself, so it fires on every run.

ADVISORY-FIRST: scorecard `passed=True`; non-empty `field_errors` list the fixes.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _load_map(path: str) -> dict[str, str]:
    import yaml
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return dict(data.get("normalizable_fields") or {})


def _nonempty_str(v: Any) -> bool:
    return isinstance(v, str) and v.strip() != ""


def _walk_fields(obj: Any, ref: str, field_to_key: dict[str, str]):
    """Yield (ref, leaf_key, value) for every leaf field named in field_to_key."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "occurrences":
                continue                       # never normalize the verbatim record
            cref = f"{ref}.{k}"
            if isinstance(v, (dict, list)):
                yield from _walk_fields(v, cref, field_to_key)
            elif k in field_to_key and _nonempty_str(v):
                yield cref, k, v
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, (dict, list)):
                yield from _walk_fields(v, f"{ref}[{i}]", field_to_key)


class NormalizationVerifier:
    def __init__(self, *, mapping_path: str = "config/normalizer_map.yaml",
                 normalizers: dict[str, Callable[[str], dict[str, Any]]] | None = None) -> None:
        self._map = _load_map(mapping_path)
        if normalizers is None:
            from agents.normalizer_hooks import build_normalizers
            normalizers = build_normalizers()
        self._normalizers = normalizers

    def verify(self, *, envelope: dict[str, Any]) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        for sec, payload in (envelope or {}).items():
            if not isinstance(payload, (dict, list)):
                continue
            for ref, leaf, value in _walk_fields(payload, sec, self._map):
                key = self._map[leaf]
                fn = self._normalizers.get(key)
                if fn is None:
                    continue
                try:
                    res = fn(value) or {}
                except Exception:  # noqa: BLE001
                    logger.exception("normalizer %s failed on %s", key, ref)
                    continue
                canon = res.get("value")
                # flag only when there's a clean canonical that DIFFERS from the value
                if res.get("matched") and canon not in (None, "") and str(canon) != str(value):
                    errors.append({
                        "ref": ref, "field_name": ref, "normalizer_key": key,
                        "input": value, "canonical": canon, "status": "renormalizable",
                    })

        notes = (f"{len(errors)} field(s) renormalizable (canonical differs from extracted)."
                 if errors else "no renormalizations needed")
        return {"verifier_name": "normalization", "passed": True,
                "field_errors": errors[:100], "notes": notes}
