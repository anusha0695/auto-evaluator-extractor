"""
V4-M12 gate — production output = mCODE genomic_pathology_extraction (identity_v4).

The customer's production target IS the mCODE schema (== our v4 extraction schema),
so to_production in mode: identity_v4 returns the v4 envelope with internal-only keys
stripped, wrapped under `genomic_pathology_extraction`.

Locks (offline, deterministic; to_production is pure):
  [1] default mode is identity_v4 → output root is `genomic_pathology_extraction`
      (NOT the legacy `pathology_extraction`).
  [2] only the configured `sections` are emitted, and the clinical content is
      copied IDENTITY (gene_studied, coding_dna_change, … unchanged).
  [3] internal-only keys (provenance, needs_review, review_reason, hgvs_normalized,
      occurrences, vmaw_*) are stripped at every nesting level.
  [4] production_label_identity maps a surviving field to its own path and a
      stripped key to None.
  [5] the legacy pathology_extraction transform is still reachable via
      mode: pathology_extraction (revert path intact).

Run:  PYTHONPATH=. python scripts/gates/gate_v4_m12_production_identity.py
"""

from __future__ import annotations

import json
import sys
import tempfile

import yaml

from transform.to_production import production_label_identity, to_production

_ENV = {
    "count_of_extracted_objects": 2,
    "report_metadata": {
        "provenance": [{"field_name": "Patient_First_Name", "rationale": "block 60"}],
        "Patient_First_Name": "William", "Patient_Last_Name": "Dawson",
        "llm_confidence_score": 0.98,
    },
    "Genomic_Variant_umbrella": {
        "count_of_Genomic_Variants": 1, "llm_confidence_score": 0.9,
        "Genomic_Variants": [{
            "gene_studied": "JAK2", "coding_dna_change": "1849G>T",
            "amino_acid_change": "V617F", "result": "Not Detected",
            "needs_review": None, "review_reason": None,
            "hgvs_normalized": {"coding": "c.1849G>T", "valid": True},
            "provenance": [{"field_name": "gene_studied", "rationale": "block 10"}],
            "BOGUS_EXTRA": "should be dropped (not in mCODE schema)",
        }],
    },
    "other_molecular_biomarker_umbrella": {
        "count_of_other_molecular_biomarkers": 0, "llm_confidence_score": 1.0,
        "other_molecular_biomarkers": [],
    },
    "tested_biomarker_umbrella": {
        "count_of_tested_biomarkers": 1, "page_numbers": [2],
        "llm_confidence_score": 0.95, "tested_biomarkers": ["JAK2"],
    },
}


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    print("[1] default mode = identity_v4 → mCODE root key")
    out = to_production(_ENV)
    check("root is genomic_pathology_extraction", list(out.keys()) == ["genomic_pathology_extraction"],
          str(list(out.keys())))
    inner = out["genomic_pathology_extraction"]

    print("[2] only configured sections emitted + clinical content is identity")
    check("emits report_metadata + variant + biomarker + tested + count",
          {"count_of_extracted_objects", "report_metadata", "Genomic_Variant_umbrella",
           "other_molecular_biomarker_umbrella", "tested_biomarker_umbrella"} == set(inner),
          str(sorted(inner)))
    gv = inner["Genomic_Variant_umbrella"]["Genomic_Variants"][0]
    check("gene_studied copied identity", gv.get("gene_studied") == "JAK2")
    check("coding_dna_change copied identity (verbatim, no fold)", gv.get("coding_dna_change") == "1849G>T")
    check("tested_biomarkers copied identity", inner["tested_biomarker_umbrella"]["tested_biomarkers"] == ["JAK2"])

    print("[3] internal-only keys stripped at every level")
    check("variant.provenance stripped", "provenance" not in gv)
    check("variant.needs_review stripped", "needs_review" not in gv)
    check("variant.review_reason stripped", "review_reason" not in gv)
    check("variant.hgvs_normalized stripped", "hgvs_normalized" not in gv)
    check("report_metadata.provenance stripped", "provenance" not in inner["report_metadata"])

    print("[3b] schema completion — every mCODE field present (absent → null), per the "
          "mCODE 'every field must appear' rule (honors required)")
    schema = json.load(open("config/schemas/genomic_pathology_v4.json", encoding="utf-8"))
    strip_set = {"provenance", "needs_review", "review_reason", "hgvs_normalized",
                 "occurrences", "vmaw_confirmed", "vmaw_attribution_confirmed"}
    rm_fields = [f for f in schema["properties"]["report_metadata"]["properties"]
                 if f not in strip_set]
    rm = inner["report_metadata"]
    check("every mCODE report_metadata field present",
          all(f in rm for f in rm_fields), str([f for f in rm_fields if f not in rm]))
    check("omitted metadata field appears as null (Patient_MRN)",
          rm.get("Patient_MRN", "MISSING") is None)
    gv_fields = [f for f in schema["properties"]["Genomic_Variant_umbrella"]
                 ["properties"]["Genomic_Variants"]["items"]["properties"] if f not in strip_set]
    check("every mCODE variant field present (omitted → null)",
          all(f in gv for f in gv_fields), str([f for f in gv_fields if f not in gv]))
    check("omitted variant field appears as null (variant_allele_frequency)",
          gv.get("variant_allele_frequency", "MISSING") is None)
    check("non-schema key dropped", "BOGUS_EXTRA" not in gv)

    print("[4] production_label_identity")
    check("surviving field → its own path",
          production_label_identity("Genomic_Variant_umbrella.Genomic_Variants[0].gene_studied")
          == "Genomic_Variant_umbrella › Genomic_Variants[0] › gene_studied")
    check("stripped key → None",
          production_label_identity("Genomic_Variant_umbrella.Genomic_Variants[0].provenance") is None)

    print("[5] legacy pathology_extraction transform still reachable")
    m = yaml.safe_load(open("config/production_mapping.yaml", encoding="utf-8")) or {}
    m["mode"] = "pathology_extraction"
    fd = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    yaml.safe_dump(m, fd); fd.close()
    legacy = to_production(_ENV, mapping_path=fd.name)
    check("legacy mode → pathology_extraction root", "pathology_extraction" in legacy,
          str(list(legacy.keys())))

    print("[6] supersession filter — variant_superseded_by drops the original record")
    print("    so consumers see only the amended value (original 9% VAF dropped, amended 10% kept)")
    sup_env = {
        "report_metadata": {"Patient_First_Name": "Test"},
        "Genomic_Variant_umbrella": {
            "count_of_Genomic_Variants": 2,
            "Genomic_Variants": [
                {"gene_studied": "JAK2", "amino_acid_change": "V617F",
                 "variant_allele_frequency": "9%"},   # original — should be DROPPED
                {"gene_studied": "JAK2", "amino_acid_change": "V617F",
                 "variant_allele_frequency": "10%"},  # amended — should be KEPT
            ],
        },
        "other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": []},
        "tested_biomarker_umbrella": {"tested_biomarkers": []},
        "links": [{
            "from_ref": "Genomic_Variant_umbrella.Genomic_Variants[0]",
            "to_ref":   "Genomic_Variant_umbrella.Genomic_Variants[1]",
            "type":     "variant_superseded_by", "method": "deterministic",
        }],
    }
    sup_out = to_production(sup_env)["genomic_pathology_extraction"]
    sup_gv = sup_out["Genomic_Variant_umbrella"]
    check("superseded record removed (1 variant remains, not 2)",
          len(sup_gv["Genomic_Variants"]) == 1,
          f"got {len(sup_gv['Genomic_Variants'])} variants")
    check("surviving record carries the amended value (10%, not 9%)",
          sup_gv["Genomic_Variants"][0].get("variant_allele_frequency") == "10%",
          str(sup_gv["Genomic_Variants"][0].get("variant_allele_frequency")))
    check("count_of_Genomic_Variants updated to match",
          sup_gv.get("count_of_Genomic_Variants") == 1,
          str(sup_gv.get("count_of_Genomic_Variants")))
    # And without the link, both records flow through — proves the filter is
    # triggered by the link, not by content.
    no_link_env = {**sup_env, "links": []}
    no_link_out = to_production(no_link_env)["genomic_pathology_extraction"]
    check("absent link → both records flow through (filter is link-driven)",
          len(no_link_out["Genomic_Variant_umbrella"]["Genomic_Variants"]) == 2)

    print("-" * 60)
    if fails:
        print(f"V4-M12 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("V4-M12 VERIFY: PASS — production = mCODE identity_v4; internal keys stripped; legacy reachable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
