"""Canonical-form helper for cell comparison.

The eval module compares SME-authored ground-truth (often clinical-canonical)
against extractor output (verbatim from source). To handle the legitimate
surface-form variance between the two — `MSI` vs `MICROSATELLITE INSTABILITY`,
`p.G12D` vs `G12D`, `HER2` vs `ERBB2` — we run BOTH sides through the same
canonicalization pipeline before equality testing.

All canonicalization REUSES existing pipeline infra:
  • `agents.linker._canon_change`        → strip HGVS prefix (p./c./g./...)
  • `preprocess.hgnc_resolver.HGNCResolver` → gene aliases (HER2 ↔ ERBB2)
  • `config/data/biomarker_synonyms.yaml`  → biomarker aliases (MSI ↔ MSS ↔ ...)

No new alias YAMLs in the eval module — that would be duplicate truth.

VAF rule (user-confirmed): strip-numeric. A numeric-looking value with an
optional `%` suffix has the `%` stripped; the remainder is compared strictly
as a string (so `9.8` ≠ `10`, but `10` == `10%`).
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


_NUMERIC_VAF_RE = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*%?\s*$")


def _try_import_hgnc():
    """Lazy-load HGNCResolver from preprocess. Returns the canonical() callable
    or None on import failure (so this module degrades gracefully if the
    resolver isn't available)."""
    try:
        from preprocess.hgnc_resolver import HGNCResolver  # noqa: WPS433
        # The resolver may be either a class with classmethod `canonical` or a
        # module-level singleton. Try the common shapes.
        if hasattr(HGNCResolver, "canonical") and callable(HGNCResolver.canonical):
            return HGNCResolver.canonical
        # Instance-based fallback
        resolver = HGNCResolver()
        if hasattr(resolver, "canonical"):
            return resolver.canonical
    except Exception as exc:  # noqa: BLE001 — degrade
        logger.debug("HGNC resolver unavailable: %s", exc)
    return None


def _try_import_canon_change():
    """Lazy-load _canon_change from agents.linker. Strips HGVS prefix
    (p./c./g./n./r./m./o.) and uppercases."""
    try:
        from agents.linker import _canon_change  # noqa: WPS433
        return _canon_change
    except Exception as exc:  # noqa: BLE001
        logger.debug("agents.linker._canon_change unavailable: %s", exc)
    return None


def _try_import_biomarker_synonyms() -> dict[str, str]:
    """Load `config/data/biomarker_synonyms.yaml` into a flat
    `{alias_upper: canonical_upper}` map. Returns {} on any failure.

    Three YAML shapes supported (all already in the wild somewhere in this
    repo's history):

    1. The shape THIS repo actually uses today — `biomarkers:` with each
       canonical-name key holding a list of aliases:
            biomarkers:
              MSI:
                - microsatellite instability
                - msi status
              TMB: [tumor mutational burden, ...]

    2. Flat `synonyms:` alias→canonical map (one alias per line):
            synonyms:
              MSI: MICROSATELLITE INSTABILITY

    3. `groups:` list of {canonical, aliases[]}:
            groups:
              - canonical: ERBB2
                aliases: [HER2, HER2/NEU, ERBB-2]

    In all cases the output is identical: an `alias_upper → canonical_upper`
    lookup table where every canonical also resolves to itself."""
    out: dict[str, str] = {}
    try:
        import yaml
        from pathlib import Path
        p = Path("config/data/biomarker_synonyms.yaml")
        if not p.is_file():
            return {}
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

        # Shape 1: `biomarkers:` mapping canonical → [aliases]   (this repo)
        if isinstance(raw.get("biomarkers"), dict):
            for canonical_name, aliases in raw["biomarkers"].items():
                canon_u = str(canonical_name).strip().upper()
                if not canon_u:
                    continue
                out[canon_u] = canon_u                     # canonical → itself
                if isinstance(aliases, (list, tuple)):
                    for alias in aliases:
                        a = str(alias).strip().upper()
                        if a:
                            out[a] = canon_u
                elif isinstance(aliases, str):             # tolerate single alias as string
                    a = aliases.strip().upper()
                    if a:
                        out[a] = canon_u

        # Shape 2: flat `synonyms:` mapping alias → canonical
        if isinstance(raw.get("synonyms"), dict):
            for k, v in raw["synonyms"].items():
                out[str(k).strip().upper()] = str(v).strip().upper()

        # Shape 3: `groups:` list of {canonical, aliases}
        for grp in (raw.get("groups") or []):
            if not isinstance(grp, dict):
                continue
            canon = str(grp.get("canonical") or "").strip().upper()
            if not canon:
                continue
            out[canon] = canon
            for alias in (grp.get("aliases") or []):
                out[str(alias).strip().upper()] = canon
    except Exception as exc:  # noqa: BLE001
        logger.debug("biomarker synonyms load failed: %s", exc)
    return out


# Module-level lazy imports — resolved once per process.
_HGNC_CANONICAL = _try_import_hgnc()
_CANON_CHANGE = _try_import_canon_change()
_BIOMARKER_SYNONYMS = _try_import_biomarker_synonyms()


def canonical(value: Any) -> str:
    """Canonicalize a cell value for comparison.

    Pipeline (each step is no-op if it doesn't apply):
      1. coerce to string, strip whitespace
      2. if it looks like a numeric VAF (`/^\\d+(\\.\\d+)?\\s*%?$/`),
         strip `%` and return the bare numeric string (preserves precision)
      3. uppercase
      4. strip HGVS prefix via `agents.linker._canon_change`
      5. resolve biomarker synonym to canonical (MSI → MICROSATELLITE INSTABILITY)
      6. resolve HGNC gene alias (HER2 → ERBB2)

    Returns "" for empty / None values.
    """
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""

    # (2) Numeric VAF — strict-numeric per user rule. Returns the numeric core
    # without `%`. So "10" == canonical("10%") == "10", and "9.8" != "10".
    m = _NUMERIC_VAF_RE.match(s)
    if m:
        return m.group(1)

    s = s.upper()

    # (4) HGVS prefix strip
    if _CANON_CHANGE is not None:
        try:
            s = _CANON_CHANGE(s) or s
        except Exception:  # noqa: BLE001 — fall back to s
            pass

    # (5) Biomarker synonym → canonical
    if _BIOMARKER_SYNONYMS:
        s = _BIOMARKER_SYNONYMS.get(s, s)

    # (6) HGNC gene alias → canonical
    if _HGNC_CANONICAL is not None:
        try:
            canon = _HGNC_CANONICAL(s)
            if canon:
                s = str(canon).strip().upper()
        except Exception:  # noqa: BLE001
            pass

    return s
