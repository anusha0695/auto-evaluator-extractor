"""
V4-M1 gate — genomic_pathology_v4.json sections + section enable/disable toggle.

Locks (offline, deterministic):
  [1] schema_loader builds a Pydantic model for every v4 section; the 4 genomic
      sections (+ count) are REQUIRED, significant_findings + clinical_information are
      OPTIONAL (disabled-by-default per D3 — absence must validate).
  [2] Genomic_Variant_umbrella is a SEPARATE top-level array carrying `gene_studied`
      (un-merged from biomarker findings).
  [3] other_molecular_biomarker_umbrella is FLAT (no findings/occurrences/variant_detail).
  [4] tested_biomarker_umbrella is the alphabetized unique name list.
  [5] core/section_toggle: absent/true → enabled; explicit false → disabled; and
      disabled_sections() reports the disabled team's schema_section.

Run:  PYTHONPATH=. python scripts/gates/gate_v4_m1_schema_sections.py
"""

from __future__ import annotations

import sys

from core.schema_loader import SchemaLoader
from core.section_toggle import disabled_sections, enabled_team_keys, is_team_enabled

_SCHEMA = "config/schemas/genomic_pathology_v4.json"

_RM = {
    "total_pages": 1, "report_title": None, "llm_confidence_score": 0.9,
    "Patient_MRN": None, "Patient_First_Name": None, "Patient_Last_Name": None,
    "Patient_DOB": None, "Vendor_Name": None, "Collection_Date": None,
    "Received_Date": None, "Report_Date": None, "Test_Name": None, "Procedure": None,
    "Ordering_Provider_First_Name": None, "Ordering_Provider_Last_Name": None,
    "Ordering_Provider_NPI": None, "Ordering_Provider_Title": None,
    "Practice_Name": None, "Practice_NPI": None, "Practice_Street_Address_1": None,
    "Practice_Street_Address_2": None, "Practice_City": None, "Practice_State": None,
    "Practice_ZIP_5digit": None, "Practice_ZIP_4digit": None,
}


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    print("[1] schema loads + every section builds a model")
    sl = SchemaLoader.from_path(_SCHEMA)
    secs = sl.list_sections()
    expect = {"report_metadata", "Genomic_Variant_umbrella",
              "other_molecular_biomarker_umbrella", "tested_biomarker_umbrella",
              "significant_findings", "clinical_information"}
    check("all 6 sections present", expect <= set(secs), str(secs))
    built = []
    for s in secs:
        try:
            sl.get_section(s); built.append(s)
        except Exception as exc:  # noqa: BLE001
            check(f"model builds: {s}", False, str(exc))
    check("models built for every section", len(built) == len(secs), f"{len(built)}/{len(secs)}")

    root = sl.get_root_schema()
    req = set(root.get("required") or [])
    check("4 genomic sections + count are REQUIRED",
          {"count_of_extracted_objects", "report_metadata", "Genomic_Variant_umbrella",
           "other_molecular_biomarker_umbrella", "tested_biomarker_umbrella"} <= req, str(req))
    check("significant_findings + clinical_information are OPTIONAL (D3 disable-able)",
          "significant_findings" not in req and "clinical_information" not in req)

    print("[2] Genomic_Variant_umbrella — separate section, gene_studied")
    gv_props = root["properties"]["Genomic_Variant_umbrella"]["properties"]["Genomic_Variants"]["items"]["properties"]
    check("variant item has gene_studied", "gene_studied" in gv_props)
    check("variant carries HGVS fields", {"coding_dna_change", "amino_acid_change", "genomic_dna_change"} <= set(gv_props))
    sl.validate("Genomic_Variant_umbrella", {
        "count_of_Genomic_Variants": 1, "llm_confidence_score": 0.9,
        "Genomic_Variants": [{"gene_studied": "JAK2", "method": "PCR",
                              "result": "Detected", "amino_acid_change": "V617F"}]})
    check("Genomic_Variant_umbrella validates", True)

    print("[3] other_molecular_biomarker_umbrella — FLAT")
    omb_props = set(root["properties"]["other_molecular_biomarker_umbrella"]["properties"]["other_molecular_biomarkers"]["items"]["properties"])
    check("biomarker item is flat (no findings/variant_detail)",
          "findings" not in omb_props and "variant_detail" not in omb_props, str(sorted(omb_props)))
    check("biomarker has the 6 prod fields",
          {"page_number", "biomarker_name", "method", "result", "reference_range", "interpretation"} <= omb_props)
    sl.validate("other_molecular_biomarker_umbrella", {
        "count_of_other_molecular_biomarkers": 1, "llm_confidence_score": 0.9,
        "other_molecular_biomarkers": [{"page_number": 2, "biomarker_name": "ER",
                                        "method": "IHC", "result": "0%",
                                        "reference_range": None, "interpretation": "negative"}]})
    check("flat biomarker validates", True)

    print("[4] tested_biomarker_umbrella")
    sl.validate("tested_biomarker_umbrella", {
        "count_of_tested_biomarkers": 2, "page_numbers": [1],
        "llm_confidence_score": 0.9, "tested_biomarkers": ["BRAF", "JAK2"]})
    check("tested_biomarker validates", True)

    print("[5] section enable/disable toggle (D3)")
    check("absent enabled → enabled", is_team_enabled({"schema_section": "x"}) is True)
    check("enabled:true → enabled", is_team_enabled({"enabled": True}) is True)
    check("enabled:false → disabled", is_team_enabled({"enabled": False}) is False)
    check("string 'false' → disabled", is_team_enabled({"enabled": "false"}) is False)
    cfg = {"teams": {
        "metadata_team": {"schema_section": "report_metadata"},
        "specimen_findings_team": {"schema_section": "significant_findings", "enabled": False},
        "clinical_info_team": {"schema_section": "clinical_information", "enabled": False},
    }}
    check("enabled_team_keys drops disabled",
          enabled_team_keys(cfg) == ["metadata_team"], str(enabled_team_keys(cfg)))
    check("disabled_sections reports both disabled sections",
          disabled_sections(cfg) == {"significant_findings", "clinical_information"},
          str(disabled_sections(cfg)))

    print("-" * 60)
    if fails:
        print(f"V4-M1 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("V4-M1 VERIFY: PASS — v4 schema sections + Pydantic gen + section toggle.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
