"""
to_production — convert our v3 extraction envelope → the production
`pathology_extraction` shape (P3-M9). Pure / offline.

Reviewed mapping lives in PRODUCTION_MAPPING.md + config/production_mapping.yaml.
Decisions locked with the user:
  A. emit the narrative section under key `significant_findings`.
  B. orphan report_metadata fields (Test_Name/Procedure/section confidence) kept
     under `administrative_info._extra` (clearly non-production).
  C. a biomarker is "with definitive result" if any finding has a non-null `result`
     (negatives included).
  D. `variant_detail` is FOLDED into the production `details` string (no data loss).
"""

from __future__ import annotations

from typing import Any

import yaml

DEFAULT_MAPPING = "config/production_mapping.yaml"

# keys that are ours-only / internal — stripped from significant_findings + clinical
_STRIP = {"provenance", "needs_review", "review_reason", "occurrences",
          "vmaw_confirmed", "vmaw_attribution_confirmed"}

# variant_detail → details fold, in clinical priority order (verbatim key, label)
_VARIANT_ORDER = [
    ("amino_acid_change", ""), ("coding_dna_change", "c. "), ("genomic_dna_change", "g. "),
    ("exon", "exon "), ("variant_allele_frequency", "VAF "),
    ("clinical_significance", ""), ("genomic_source_class", ""), ("allelic_state", ""),
]


def to_production(envelope: dict[str, Any], *, mapping_path: str = DEFAULT_MAPPING) -> dict[str, Any]:
    """Return the production `{"pathology_extraction": {...}}` object."""
    mapping = yaml.safe_load(open(mapping_path, encoding="utf-8")) or {}
    rm = envelope.get("report_metadata") or {}
    return {"pathology_extraction": {
        "administrative_info": _resolve(mapping.get("administrative_info") or {}, rm),
        "significant_findings": _significant_findings(envelope.get("significant_findings") or {}),
        "clinical_information": _strip_keys(envelope.get("clinical_information") or {}),
        "pathology_biomarkers_findings": _biomarkers_findings(
            envelope.get("other_molecular_biomarker_umbrella") or {}),
        "pathology_biomarkers_mentioned": _biomarkers_mentioned(
            envelope.get("tested_biomarker_umbrella") or {}),
    }}


# -- administrative_info (driven by the reviewed YAML map) ------------------


def _resolve(node: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
    """Walk the nested rename map: str leaf → src[value]; dict → recurse."""
    out: dict[str, Any] = {}
    for target_key, spec in node.items():
        if isinstance(spec, dict):
            out[target_key] = _resolve(spec, src)
        else:
            out[target_key] = src.get(spec)
    return out


# -- significant_findings + clinical_information (≈ 1:1, strip internal keys) -


def _strip_keys(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_keys(v) for k, v in obj.items() if k not in _STRIP}
    if isinstance(obj, list):
        return [_strip_keys(v) for v in obj]
    return obj


def _significant_findings(section: dict[str, Any]) -> dict[str, Any]:
    # production shape is just {"specimen_findings": [...]} — drop our count + section conf
    return {"specimen_findings": _strip_keys(section.get("specimen_findings") or [])}


# -- pathology_biomarkers_findings (filter + flatten + fold variant) --------


def _fold_variant(details: Any, vd: Any) -> str | None:
    pieces: list[str] = []
    if isinstance(details, str) and details.strip():
        pieces.append(details.strip())
    if isinstance(vd, dict):
        for key, label in _VARIANT_ORDER:
            val = vd.get(key)
            if val not in (None, ""):
                pieces.append(f"{label}{val}")
        norm = vd.get("hgvs_normalized") if isinstance(vd.get("hgvs_normalized"), dict) else {}
        if not vd.get("amino_acid_change") and norm.get("amino_acid"):
            pieces.append(str(norm["amino_acid"]))
        if not vd.get("coding_dna_change") and norm.get("coding"):
            pieces.append(str(norm["coding"]))   # normalized form already carries the c. prefix
    if not pieces:
        return details if details not in (None, "") else None
    return "; ".join(pieces)


def _biomarkers_findings(umb: dict[str, Any]) -> dict[str, Any]:
    included: list[dict[str, Any]] = []
    pages: set[int] = set()
    for bm in (umb.get("other_molecular_biomarkers") or []):
        flat: list[dict[str, Any]] = []
        for f in (bm.get("findings") or []):
            if f.get("result") in (None, ""):           # Decision C: definitive = non-null result
                continue
            flat.append({
                "result": f.get("result"), "method": f.get("method"),
                "details": _fold_variant(f.get("details"), f.get("variant_detail")),  # Decision D
                # interpretation ← our `assertion` (affirmed/negated/uncertain/...),
                # falling back to our printed `interpretation` when assertion is absent.
                "interpretation": f.get("assertion") or f.get("interpretation"),
            })
        if not flat:
            continue
        if isinstance(bm.get("page_number"), int):
            pages.add(bm["page_number"])
        included.append({"page_number": bm.get("page_number"),
                         "biomarker_name": bm.get("biomarker_name"), "findings": flat})
    return {
        "page_numbers": _pages_str(pages),
        "llm_confidence_score": umb.get("llm_confidence_score"),
        "number_of_biomarkers_with_definitive_results": len(included),
        "pathology_biomarkers": included,
    }


# -- pathology_biomarkers_mentioned -----------------------------------------


def _biomarkers_mentioned(tb: dict[str, Any]) -> dict[str, Any]:
    tested = tb.get("tested_biomarkers") or []
    no_res = tb.get("biomarkers_tested_no_result") or []
    return {
        "biomarkers_tested_with_result": {
            "page_numbers": _pages_str(tb.get("page_numbers") or []),
            "llm_confidence_score": tb.get("llm_confidence_score"),
            "number_of_biomarkers_tested_with_result": len(tested),
            "biomarkers_tested_with_result": [{"biomarker_name": s} for s in tested],
        },
        "biomarkers_tested_no_result": {
            "page_numbers": None,
            "llm_confidence_score": tb.get("llm_confidence_score"),
            "number_of_biomarkers_tested_no_result": len(no_res),
            "biomarkers_tested_no_results": [
                {"name": x.get("name"), "reason": x.get("reason"), "details": x.get("details")}
                for x in no_res
            ],
        },
    }


# -- v3-ref → production location (for the Production browser UI) -----------


def admin_inverse(mapping: dict[str, Any]) -> dict[str, str]:
    """{ source report_metadata field → 'administrative_info › group › prod_field' }."""
    inv: dict[str, str] = {}
    for k, v in (mapping.get("administrative_info") or {}).items():
        if isinstance(v, dict):                 # group: patient_info / ordering_provider / practice / _extra
            for prod_key, src in v.items():
                inv[src] = f"administrative_info › {k} › {prod_key}"
        else:
            inv[v] = f"administrative_info › {k}"
    return inv


def production_label(ref: str, admin_inv: dict[str, str]) -> str | None:
    """Map an OUR-envelope field ref → its production location label, or None if the
    field does NOT survive into the production schema (so the UI can hide it)."""
    parts = str(ref).split(".")
    sec = parts[0]
    leaf = parts[-1].split("[")[0]
    if sec == "report_metadata":
        return admin_inv.get(leaf)              # None for provenance.* etc.
    if sec == "significant_findings":
        return "significant_findings › " + ".".join(parts[1:]) if len(parts) > 1 else "significant_findings"
    if sec == "clinical_information":
        return "clinical_information › " + ".".join(parts[1:]) if len(parts) > 1 else "clinical_information"
    if sec == "other_molecular_biomarker_umbrella":
        if leaf == "biomarker_name":
            return "pathology_biomarkers_findings › biomarker_name"
        if leaf in ("result", "method", "details"):
            return f"pathology_biomarkers_findings › findings › {leaf}"
        if leaf == "assertion":
            return "pathology_biomarkers_findings › findings › interpretation (← assertion)"
        if leaf == "interpretation":
            return "pathology_biomarkers_findings › findings › interpretation"
        if "variant_detail" in ref:
            return "pathology_biomarkers_findings › findings › details (folded)"
        return None                             # biomarker_class, specimen_id, method_source_type, … dropped
    if sec == "tested_biomarker_umbrella":
        if "biomarkers_tested_no_result" in ref:
            return f"pathology_biomarkers_mentioned › tested_no_result › {leaf}"
        if "tested_biomarkers" in ref:
            return "pathology_biomarkers_mentioned › biomarkers_tested_with_result"
        return None
    return None


def _pages_str(pages: Any) -> str | None:
    nums = sorted({int(p) for p in (pages or []) if isinstance(p, (int, float))})
    return ", ".join(str(n) for n in nums) if nums else None
