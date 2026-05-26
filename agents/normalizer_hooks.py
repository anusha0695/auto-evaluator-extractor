"""
Normalizer adapters (gap #2) — uniform {value, matched} wrappers over the M2
deterministic normalizers, shared by the NormalizationVerifier (detect) and the
RepairExecutor (apply). Pure / offline (no LLM, no network).

Each adapter: `fn(raw: str) -> {"value": <canonical or None>, "matched": bool}`.
`matched=True` means we have a display-correct canonical safe to write back.
"""

from __future__ import annotations

from typing import Any, Callable


def build_normalizers() -> dict[str, Callable[[str], dict[str, Any]]]:
    """{normalizer_key → adapter}. Lazy imports keep this offline-safe."""

    def _biomarker(raw: str) -> dict[str, Any]:
        from preprocess.normalizers import biomarker_normalize
        r = biomarker_normalize(raw) or {}
        return {"value": r.get("canonical"), "matched": bool(r.get("matched"))}

    def _method(raw: str) -> dict[str, Any]:
        from preprocess.normalizers import method_normalize
        r = method_normalize(raw) or {}
        return {"value": r.get("canonical"), "matched": bool(r.get("matched"))}

    def _hgvs(raw: str) -> dict[str, Any]:
        from preprocess.hgvs_validate import hgvs_validate
        r = hgvs_validate(raw) or {}
        norm = r.get("normalized")
        return {"value": norm, "matched": bool(r.get("valid") and norm)}

    return {"biomarker": _biomarker, "method": _method, "hgvs": _hgvs}
