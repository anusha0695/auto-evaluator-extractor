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

# Identity-v4 mode: the customer's production target IS the mCODE
# genomic_pathology_extraction schema (== our v4 extraction schema), so production
# output is the v4 envelope with internal-only keys stripped, wrapped under the
# mCODE output key. The strip set + section list come from production_mapping.yaml
# (mode: identity_v4) so the contract is config-driven; this is the fallback.
_IDENTITY_STRIP_DEFAULT = {
    "provenance", "needs_review", "review_reason", "hgvs_normalized",
    "occurrences", "vmaw_confirmed", "vmaw_attribution_confirmed",
}
_IDENTITY_SECTIONS_DEFAULT = [
    "count_of_extracted_objects", "report_metadata", "Genomic_Variant_umbrella",
    "other_molecular_biomarker_umbrella", "tested_biomarker_umbrella",
]
_MCODE_ROOT_KEY = "genomic_pathology_extraction"

# variant_detail → details fold, in clinical priority order (verbatim key, label)
_VARIANT_ORDER = [
    ("amino_acid_change", ""), ("coding_dna_change", "c. "), ("genomic_dna_change", "g. "),
    ("exon", "exon "), ("variant_allele_frequency", "VAF "),
    ("clinical_significance", ""), ("genomic_source_class", ""), ("allelic_state", ""),
]


def to_production(envelope: dict[str, Any], *, mapping_path: str = DEFAULT_MAPPING) -> dict[str, Any]:
    """Convert our extraction envelope → the production object.

    Dispatches on the mapping's `mode`:
      • identity_v4         → the v4 envelope with internal keys stripped, wrapped
                              under `genomic_pathology_extraction` (the mCODE contract).
                              This is the current production target.
      • pathology_extraction (default when mode absent) → the legacy custom
                              `pathology_extraction` shape (rename / fold / flatten).
                              Kept reachable so flipping the mapping's `mode` reverts.
    """
    mapping = yaml.safe_load(open(mapping_path, encoding="utf-8")) or {}
    mode = mapping.get("mode", "pathology_extraction")
    if mode == "identity_v4":
        return _identity_v4(envelope, mapping)
    return _legacy_pathology_extraction(envelope, mapping)


# Default schema path. Overridable WITHOUT touching Python: set `schema:` in
# config/production_mapping.yaml to point at any schema file (e.g. a future v5).
# This constant is only the fallback when the mapping omits `schema:`.
_V4_SCHEMA_PATH = "config/schemas/genomic_pathology_v4.json"


def _load_v4_properties(schema_path: str = _V4_SCHEMA_PATH) -> dict[str, Any]:
    """Top-level `properties` of the production-contract schema. `schema_path`
    comes from the mapping's `schema:` key, so a schema swap is config-only."""
    import json
    with open(schema_path, encoding="utf-8") as fh:
        return (json.load(fh) or {}).get("properties") or {}


def _identity_v4(envelope: dict[str, Any], mapping: dict[str, Any]) -> dict[str, Any]:
    """Production = the envelope conformed to the production-contract schema:
      • every field DEFINED in the schema is present (missing → null), honoring the
        mCODE rule "every field must appear; absent → null" (covers `required`).
      • internal-only keys (provenance, needs_review, hgvs_normalized, …) are stripped.
      • keys NOT in the schema are dropped (the output is exactly the schema shape).
    Pure / offline. The schema file, sections, and strip keys all come from
    config/production_mapping.yaml — no Python edit needed to retarget."""
    props = _load_v4_properties(mapping.get("schema") or _V4_SCHEMA_PATH)
    strip = set(mapping.get("strip_keys") or _IDENTITY_STRIP_DEFAULT)
    # Sections DERIVE from the schema by default — every top-level property, in schema
    # order — so the mapping never goes stale: add a field or a whole section to the
    # schema and production tracks it with no edit here. `exclude_sections` suppresses
    # sections that exist in the schema but are disabled in this pipeline (their teams
    # are off in teams_v4.yaml). An explicit `sections:` list still wins if you ever
    # need to pin the set/order manually.
    exclude = set(mapping.get("exclude_sections") or [])
    sections = mapping.get("sections") or [k for k in props if k not in exclude]
    out: dict[str, Any] = {}
    for sec in sections:
        if sec not in props:
            # not a schema object (e.g. count_of_extracted_objects scalar) — copy as-is.
            if sec in envelope:
                out[sec] = envelope.get(sec)
            continue
        out[sec] = _conform(envelope.get(sec), props[sec], strip)
    return {_MCODE_ROOT_KEY: out}


def _conform(value: Any, schema_node: dict[str, Any], strip: set[str]) -> Any:
    """Shape `value` to `schema_node`, filling every defined field (missing → null),
    stripping internal-only keys, and dropping keys not in the schema."""
    node_type = schema_node.get("type")
    is_object = node_type == "object" or "properties" in schema_node
    is_array = node_type == "array" or "items" in schema_node

    if is_object and isinstance(schema_node.get("properties"), dict):
        src = value if isinstance(value, dict) else {}
        result: dict[str, Any] = {}
        for field, fnode in schema_node["properties"].items():
            if field in strip:                         # internal-only → never shipped
                continue
            result[field] = _conform(src.get(field), fnode, strip) if isinstance(fnode, dict) \
                else src.get(field)
        return result

    if is_array and isinstance(schema_node.get("items"), dict):
        items_schema = schema_node["items"]
        src_list = value if isinstance(value, list) else []
        # If items are objects, conform each; if scalars (e.g. tested_biomarkers: str), copy.
        if items_schema.get("type") == "object" or "properties" in items_schema:
            return [_conform(it, items_schema, strip) for it in src_list]
        return list(src_list)

    # scalar leaf: keep the value, or null when absent (mCODE: absent → null)
    return value if value is not None else None


def _legacy_pathology_extraction(envelope: dict[str, Any], mapping: dict[str, Any]) -> dict[str, Any]:
    """The original custom production shape (P3-M9). Reachable via mode:
    pathology_extraction — kept for revertability and the legacy gates."""
    rm = envelope.get("report_metadata") or {}
    return {"pathology_extraction": {
        "administrative_info": _resolve(mapping.get("administrative_info") or {}, rm),
        "significant_findings": _significant_findings(envelope.get("significant_findings") or {}),
        "clinical_information": _strip_keys(envelope.get("clinical_information") or {}),
        "pathology_biomarkers_findings": _biomarkers_findings(
            envelope.get("other_molecular_biomarker_umbrella") or {},
            envelope.get("Genomic_Variant_umbrella") or {}),
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


def _biomarkers_findings(umb: dict[str, Any], variant_umb: dict[str, Any] | None = None) -> dict[str, Any]:
    """Production `pathology_biomarkers_findings`. Shape-tolerant across v3 and v4:

    - v3: each `other_molecular_biomarkers[]` carries a nested `findings[]` (a sequence
      variant folds its `variant_detail` into `details`).
    - v4 (D2): `other_molecular_biomarkers[]` is FLAT (the record IS the finding — no
      findings[], no variant_detail), and gene SEQUENCE variants live in a SEPARATE
      `Genomic_Variant_umbrella` (D1). We source BOTH here so the production contract
      (one biomarker-findings section) is unchanged: flat biomarkers map 1:1, and each
      Genomic_Variant becomes a biomarker entry keyed on gene_studied with its HGVS
      folded into `details` — exactly the v3 production shape.

    v3 is byte-identical: v3 records always have findings[] so the flat branch never
    fires, and a v3 envelope has no Genomic_Variant_umbrella so no variant entries are added."""
    included: list[dict[str, Any]] = []
    pages: set[int] = set()
    for bm in (umb.get("other_molecular_biomarkers") or []):
        flat: list[dict[str, Any]] = []
        findings = bm.get("findings")
        if isinstance(findings, list) and findings:                  # v3 nested shape
            for f in findings:
                if f.get("result") in (None, ""):                    # Decision C: definitive = non-null result
                    continue
                flat.append({
                    "result": f.get("result"), "method": f.get("method"),
                    "details": _fold_variant(f.get("details"), f.get("variant_detail")),  # Decision D
                    # interpretation ← our `assertion` (affirmed/negated/uncertain/...),
                    # falling back to our printed `interpretation` when assertion is absent.
                    "interpretation": f.get("assertion") or f.get("interpretation"),
                })
        else:                                                         # v4 FLAT shape: the record IS the finding
            if bm.get("result") not in (None, ""):
                flat.append({
                    "result": bm.get("result"), "method": bm.get("method"),
                    "details": bm.get("reference_range"),
                    "interpretation": bm.get("interpretation"),
                })
        if not flat:
            continue
        if isinstance(bm.get("page_number"), int):
            pages.add(bm["page_number"])
        included.append({"page_number": bm.get("page_number"),
                         "biomarker_name": bm.get("biomarker_name"), "findings": flat})

    # v4 (D1): fold the separate Genomic_Variant_umbrella records into the SAME section.
    for v in ((variant_umb or {}).get("Genomic_Variants") or []):
        if v.get("result") in (None, ""):                            # Decision C
            continue
        if isinstance(v.get("page_number"), int):
            pages.add(v["page_number"])
        included.append({
            "page_number": v.get("page_number"),
            "biomarker_name": v.get("gene_studied"),
            "findings": [{
                "result": v.get("result"), "method": v.get("method"),
                "details": _fold_variant(None, v),                   # Decision D — fold HGVS into details
                "interpretation": v.get("clinical_significance"),
            }],
        })
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


def production_label_identity(ref: str, strip: set[str] | None = None) -> str | None:
    """Identity-v4 mode: production == the v4 envelope, so every field maps to
    ITSELF. Returns the ref rendered as a `section › path` label, or None for an
    internal-only key that's stripped from production (provenance, needs_review,
    hgvs_normalized, …) so the UI hides it."""
    strip = strip if strip is not None else _IDENTITY_STRIP_DEFAULT
    parts = str(ref).split(".")
    leaf = parts[-1].split("[")[0]
    if leaf in strip:
        return None
    # the field survives unchanged — its production location IS its envelope path
    return " › ".join(parts)


def production_label(ref: str, admin_inv: dict[str, str]) -> str | None:
    """Map an OUR-envelope field ref → its production location label, or None if the
    field does NOT survive into the production schema (so the UI can hide it).
    This is the LEGACY pathology_extraction labeler; identity-v4 uses
    production_label_identity()."""
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
    if sec == "Genomic_Variant_umbrella":                # v4 (D1): variants fold into the biomarker-findings section
        if leaf == "gene_studied":
            return "pathology_biomarkers_findings › biomarker_name"
        if leaf in ("result", "method"):
            return f"pathology_biomarkers_findings › findings › {leaf}"
        if leaf == "clinical_significance":
            return "pathology_biomarkers_findings › findings › interpretation (← clinical_significance)"
        if leaf in ("amino_acid_change", "coding_dna_change", "genomic_dna_change", "exon",
                    "variant_allele_frequency", "genomic_source_class", "allelic_state"):
            return "pathology_biomarkers_findings › findings › details (folded)"
        return None                                      # page_number, refs, needs_review, … dropped
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
