"""
Phase 3 M9 gate — production-schema conversion (transform/to_production.py). Offline.

Checks the reviewed mapping + locked decisions:
  [1] administrative_info: flat report_metadata → nested patient/provider/practice;
      orphan fields under `_extra` (Decision B).
  [2] significant_findings + clinical_information ≈ 1:1, internal keys stripped.
  [3] pathology_biomarkers_findings: include rule (non-null result incl. negatives,
      Decision C) + findings flattened + variant_detail FOLDED into details (Decision D).
  [4] pathology_biomarkers_mentioned: tested strings → {biomarker_name};
      no-result 1:1; page-list → string.
  [5] all five production top-level sections present.

Run:  PYTHONPATH=. python scripts/gates/gate_p3_m9_production_conversion.py
"""

from __future__ import annotations

import sys

import yaml

from transform.to_production import admin_inverse, production_label, to_production


def _envelope():
    return {
        "report_metadata": {
            "total_pages": 2, "report_title": "Molecular", "Report_Type": "Surgical Pathology",
            "Report_Date": "2025-08-26", "Collection_Date": "2025-08-12", "Vendor_Name": "NeoGenomics",
            "Accession_Number": "S-24-1", "Signing_Pathologist": "J. Liang",
            "Patient_First_Name": "William", "Patient_Last_Name": "Dawson", "Patient_DOB": "1941-01-25",
            "Patient_MRN": None, "Ordering_Provider_Last_Name": "Smith", "Ordering_Provider_NPI": "1234567890",
            "Additional_Provider_Name": "Dr. Jones", "Practice_Name": "Acme Path", "Practice_City": "Boston",
            "Practice_ZIP_5digit": "02115",
            "Test_Name": "JAK2 V617F", "Procedure": "PCR", "llm_confidence_score": 0.98,
            "provenance": {"Patient_First_Name": {"rationale": "x"}},
        },
        "other_molecular_biomarker_umbrella": {
            "count": 2, "llm_confidence_score": 0.9,
            "other_molecular_biomarkers": [
                {"biomarker_name": "JAK2", "biomarker_class": "sequence_variant", "page_number": 1,
                 "provenance": {}, "findings": [
                     {"method": "PCR", "result": "Detected", "details": "by quantitative PCR",
                      "interpretation": "POSITIVE", "assertion": "affirmed",
                      "variant_detail": {"amino_acid_change": "V617F", "exon": "14",
                                         "variant_allele_frequency": "12%", "clinical_significance": "Pathogenic",
                                         "hgvs_normalized": {"coding": "c.1849G>T"}}}]},
                {"biomarker_name": "BRAF", "page_number": 1, "findings": [
                     {"method": "NGS", "result": "Not Detected", "details": None, "interpretation": "NEGATIVE",
                      "variant_detail": None}]},
                {"biomarker_name": "EMPTY", "findings": [{"method": "IHC", "result": None}]},  # excluded
            ]},
        "tested_biomarker_umbrella": {
            "count_of_tested_biomarkers": 2, "page_numbers": [1, 2], "llm_confidence_score": 0.95,
            "tested_biomarkers": ["BRAF", "JAK2"],
            "biomarkers_tested_no_result": [{"name": "KRAS", "reason": "QNS", "details": "insufficient"}]},
        "significant_findings": {
            "count_of_specimen_findings": 1, "llm_confidence_score": 0.9,
            "specimen_findings": [{"provenance": {"x": 1}, "needs_review": True,
                                   "specimen": [{"specimen_id": "A", "tissue_type": "Breast"}],
                                   "gross_description": {"text": "g", "page_number": 1}}]},
        "clinical_information": {"page_number": 1, "llm_confidence_score": 0.8,
                                 "number_of_clinical_histories": 1, "reason_for_study": "rule out",
                                 "clinical_history": [{"pre_operative_diagnosis": "mass"}],
                                 "clinical_finding_details": "x", "provenance": {"reason_for_study": {}}},
    }


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    out = to_production(_envelope())
    pe = out.get("pathology_extraction") or {}

    print("[5] all five production sections present")
    check("top-level pathology_extraction", "pathology_extraction" in out)
    for sec in ("administrative_info", "significant_findings", "clinical_information",
                "pathology_biomarkers_findings", "pathology_biomarkers_mentioned"):
        check(f"section {sec}", sec in pe)

    print("[1] administrative_info reshape + _extra")
    ai = pe["administrative_info"]
    check("nested patient_info.patient_first_name", ai["patient_info"]["patient_first_name"] == "William")
    check("ordering_provider.addition_provider_name (prod spelling)",
          ai["ordering_provider"]["addition_provider_name"] == "Dr. Jones")
    check("practice.zip_5 from Practice_ZIP_5digit", ai["practice"]["zip_5"] == "02115")
    check("report_type mapped", ai["report_type"] == "Surgical Pathology")
    check("_extra holds orphan fields", ai["_extra"]["test_name"] == "JAK2 V617F"
          and ai["_extra"]["procedure"] == "PCR" and ai["_extra"]["section_llm_confidence_score"] == 0.98)
    check("provenance NOT in administrative_info", "provenance" not in ai)

    print("[2] significant_findings + clinical_information stripped")
    sf = pe["significant_findings"]
    check("significant_findings is just specimen_findings", set(sf) == {"specimen_findings"})
    rec = sf["specimen_findings"][0]
    check("internal keys stripped from specimen record", "provenance" not in rec and "needs_review" not in rec)
    check("specimen sub-data preserved", rec["specimen"][0]["tissue_type"] == "Breast")
    check("clinical_information provenance stripped", "provenance" not in pe["clinical_information"])
    check("clinical fields preserved", pe["clinical_information"]["reason_for_study"] == "rule out")

    print("[3] pathology_biomarkers_findings — filter + flatten + variant fold")
    bf = pe["pathology_biomarkers_findings"]
    names = [b["biomarker_name"] for b in bf["pathology_biomarkers"]]
    check("JAK2 + BRAF included, EMPTY excluded", names == ["JAK2", "BRAF"], str(names))
    check("count derived = 2", bf["number_of_biomarkers_with_definitive_results"] == 2)
    check("negative result included (BRAF Not Detected)", "BRAF" in names)
    jak2 = bf["pathology_biomarkers"][0]["findings"][0]
    check("finding flattened to 4 keys", set(jak2) == {"result", "method", "details", "interpretation"})
    check("interpretation ← assertion (JAK2 affirmed)", jak2["interpretation"] == "affirmed", jak2["interpretation"])
    braf = bf["pathology_biomarkers"][1]["findings"][0]
    check("interpretation falls back to printed when no assertion (BRAF)", braf["interpretation"] == "NEGATIVE", braf["interpretation"])
    check("variant_detail folded into details",
          "V617F" in jak2["details"] and "Pathogenic" in jak2["details"] and "VAF 12%" in jak2["details"]
          and "by quantitative PCR" in jak2["details"], jak2["details"])
    check("page_numbers is a string", isinstance(bf["page_numbers"], str))

    print("[4] pathology_biomarkers_mentioned")
    bm = pe["pathology_biomarkers_mentioned"]
    twr = bm["biomarkers_tested_with_result"]
    check("tested strings → {biomarker_name}", twr["biomarkers_tested_with_result"] == [{"biomarker_name": "BRAF"}, {"biomarker_name": "JAK2"}])
    check("tested count + page string", twr["number_of_biomarkers_tested_with_result"] == 2 and twr["page_numbers"] == "1, 2")
    nr = bm["biomarkers_tested_no_result"]
    check("no-result 1:1 {name,reason,details}",
          nr["biomarkers_tested_no_results"] == [{"name": "KRAS", "reason": "QNS", "details": "insufficient"}])

    print("[6] production_label (Production-browser relabel/filter)")
    inv = admin_inverse(yaml.safe_load(open("config/production_mapping.yaml", encoding="utf-8")) or {})
    check("report_metadata.Patient_First_Name → admin patient_info",
          production_label("report_metadata.Patient_First_Name", inv) == "administrative_info › patient_info › patient_first_name")
    check("orphan Test_Name → _extra", production_label("report_metadata.Test_Name", inv) == "administrative_info › _extra › test_name")
    check("biomarker finding result → pathology_biomarkers_findings",
          production_label("other_molecular_biomarker_umbrella.other_molecular_biomarkers[0].findings[0].result", inv)
          == "pathology_biomarkers_findings › findings › result")
    check("assertion → interpretation label",
          "interpretation" in (production_label("other_molecular_biomarker_umbrella.other_molecular_biomarkers[0].findings[0].assertion", inv) or ""))
    check("biomarker_class → dropped (None)",
          production_label("other_molecular_biomarker_umbrella.other_molecular_biomarkers[0].biomarker_class", inv) is None)
    check("tested_biomarkers → mentioned",
          production_label("tested_biomarker_umbrella.tested_biomarkers[0]", inv)
          == "pathology_biomarkers_mentioned › biomarkers_tested_with_result")
    check("significant_findings field keeps section prefix",
          (production_label("significant_findings.specimen_findings[0].pTNM_staging_details.pTNM_stage", inv) or "").startswith("significant_findings › "))

    print("[7] persist_v3 auto-emits extraction_production on a committed run")
    import asyncio
    from pipeline.graph_selfcorrecting import _make_selfcorrecting_persist_node

    class _FakePersistence:
        def __init__(self):
            self.artifacts: dict[str, str] = {}

        async def write_run(self, state):
            return "run-1"

        async def write_extraction(self, state, *, run_id=None):
            return "ext-1"

        async def write_artifact(self, *, doc_id, kind, content):
            self.artifacts[kind] = content

    def _persist_state(verdict):
        return {"doc_id": "gate-demo", "verdict": verdict, "extraction": _envelope(),
                "repair_log": [], "block_reads": {}, "vmaw_log": [], "agent_trace": [],
                "binding_items": [], "escalation_queue": []}

    fp = _FakePersistence()
    asyncio.run(_make_selfcorrecting_persist_node(persistence=fp)(_persist_state("auto_accept")))
    check("extraction_production artifact emitted by persist node", "extraction_production" in fp.artifacts)
    if "extraction_production" in fp.artifacts:
        _pe = (yaml.safe_load(fp.artifacts["extraction_production"]) or {}).get("pathology_extraction") or {}
        check("emitted production has all five sections",
              {"administrative_info", "significant_findings", "clinical_information",
               "pathology_biomarkers_findings", "pathology_biomarkers_mentioned"} <= set(_pe))
    fp2 = _FakePersistence()
    asyncio.run(_make_selfcorrecting_persist_node(persistence=fp2)(_persist_state("sme_flag")))
    check("no production emit when verdict is not committed", "extraction_production" not in fp2.artifacts)

    print("-" * 60)
    if fails:
        print(f"P3-M9 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("P3-M9 VERIFY: PASS — production conversion (reshape, fold, filter, mentioned).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
