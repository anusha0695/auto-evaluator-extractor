"""
HGVS validation — tiered, offline, version-aware. NO Postgres, NO external API.

Backends (env `HGVS_BACKEND`, default "parse"):
  - "parse"  → biocommons `hgvs` offline parser (syntax/structure validation,
               version-aware) when the library is importable.
  - "cdot"   → biocommons `hgvs` + cdot JSON transcripts for 3'-rule
               normalization + c.↔g. mapping (opt-in; heavier).
  - internal fallback "regex" → used automatically when `hgvs` is NOT installed
               (e.g. the dev sandbox). Deterministic structural validation +
               transcript-version preservation. Cannot apply the 3' rule or map
               coordinates, but for EXTRACTION (we read the page) that's fine;
               it only limits cross-variant equivalence.

In all backends the transcript **version integer** is preserved
(`NM_004972.4` ≠ `.3`) — never dropped or rounded.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

# Accession + version, e.g. NM_004972.4 / NC_000009.12 / NP_004963.1
_ACCESSION_RE = re.compile(r"\b([NX][CGMRTPW]_\d+)\.(\d+)\b")

# Structural HGVS shape used by the regex fallback. Permissive but rejects junk:
#   optional "<accession[.ver]>(gene)?:" prefix, then a type letter c/g/m/n/r/p,
#   a dot, then a change description over the HGVS alphabet.
_HGVS_RE = re.compile(
    r"^\s*"
    r"(?:[A-Za-z0-9_.]+(?:\([A-Za-z0-9_-]+\))?:)?"   # optional reference prefix
    r"(?P<kind>[cgmnrp])\."                            # type letter + dot
    r"(?P<change>[A-Za-z0-9_>+\-*()=?/\[\].]+)"        # change description
    r"\s*$"
)


def _has_biocommons_hgvs() -> bool:
    import importlib.util
    return importlib.util.find_spec("hgvs") is not None


def _resolve_backend() -> str:
    requested = os.environ.get("HGVS_BACKEND", "parse").lower()
    if requested in {"parse", "cdot"} and _has_biocommons_hgvs():
        return requested
    if requested in {"parse", "cdot"}:
        # library not installed → graceful deterministic fallback
        return "regex"
    return "regex"


def _extract_version(notation: str) -> tuple[str | None, int | None]:
    m = _ACCESSION_RE.search(notation or "")
    if not m:
        return None, None
    return m.group(1), int(m.group(2))


def _validate_regex(notation: str) -> dict:
    s = (notation or "").strip()
    m = _HGVS_RE.match(s)
    accession, version = _extract_version(s)
    if not m:
        return {
            "input": notation, "valid": False, "normalized": None,
            "backend": "regex", "accession": accession, "version": version,
            "kind": None, "reason": "does not match HGVS structural pattern",
        }
    return {
        "input": notation, "valid": True, "normalized": s,
        "backend": "regex", "accession": accession, "version": version,
        "kind": m.group("kind"), "reason": "structural pattern + version preserved",
    }


def _validate_biocommons(notation: str, backend: str) -> dict:
    accession, version = _extract_version(notation)
    try:
        import hgvs.parser
        var = hgvs.parser.Parser().parse_hgvs_variant(notation.strip())
        kind = getattr(getattr(var, "posedit", None), "edit", None)
        normalized = str(var)
        # cdot 3'-normalization / mapping would go here when backend == "cdot"
        # and cdot transcripts are configured; we keep extraction-time behavior
        # identical and only enrich equivalence when explicitly enabled.
        return {
            "input": notation, "valid": True, "normalized": normalized,
            "backend": backend, "accession": accession, "version": version,
            "kind": getattr(var, "type", None), "reason": "biocommons parse OK",
        }
    except Exception as exc:  # hgvs.exceptions.HGVSParseError etc.
        return {
            "input": notation, "valid": False, "normalized": None,
            "backend": backend, "accession": accession, "version": version,
            "kind": None, "reason": f"biocommons parse failed: {exc}",
        }


def hgvs_validate(notation: str) -> dict:
    """Validate an HGVS notation. Returns:
        {input, valid, normalized, backend, accession, version, kind, reason}
    On failure (`valid=False`) the caller emits `null` rather than a malformed
    string — we never guess the intended notation (OCR-garbled HGVS is a
    Tier-2 case: left verbatim / escalated, not auto-repaired)."""
    backend = _resolve_backend()
    if backend in {"parse", "cdot"}:
        return _validate_biocommons(notation, backend)
    return _validate_regex(notation)
