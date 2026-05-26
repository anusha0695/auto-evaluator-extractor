"""
Gate — production-schema parity additions (no LLM).

Checks the 6 closed gaps:
  - report_metadata gained Report_Type, Accession_Number, Signing_Pathologist,
    Ordering_Provider_Phone, Additional_Provider_Name (optional; sample validates);
  - tested_biomarker_umbrella gained biomarkers_tested_no_result[{name,reason,details}];
  - metadata + tested prompts carry the new rules;
  - the scorer no longer penalizes an extra report_metadata field absent from GT;
  - the enriched specimen_demo fixture scores all sections (admin parity fields,
    biomarker findings, tested-no-result, significant_findings) at F1 1.0 vs itself.

Run:  PYTHONPATH=. python scripts/gates/gate_p2_production_parity.py
"""

from __future__ import annotations

import copy
import json
import sys

from core.prompt_renderer import PromptRenderer, ToolDescriptor
from core.schema_loader import SchemaLoader
from scripts.score_against_ground_truth import score_all_sections, score_report_metadata


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    sl = SchemaLoader.from_path("config/schemas/genomic_pathology_v3.json")

    print("[1] schema: new admin fields + tested_no_result validate")
    md_fields = {f.name for f in sl.get_section("report_metadata").fields}
    for f in ("Report_Type", "Accession_Number", "Signing_Pathologist",
              "Ordering_Provider_Phone", "Additional_Provider_Name"):
        check(f"report_metadata has {f}", f in md_fields)
    tb_fields = {f.name for f in sl.get_section("tested_biomarker_umbrella").fields}
    check("tested umbrella has biomarkers_tested_no_result", "biomarkers_tested_no_result" in tb_fields)
    sl.validate("tested_biomarker_umbrella", {
        "count_of_tested_biomarkers": 1, "page_numbers": [1], "llm_confidence_score": 0.9,
        "tested_biomarkers": ["ER"], "biomarkers_tested_no_result": [{"name": "Ki-67", "reason": "QNS", "details": None}]})
    check("tested-no-result sample validates", True)

    print("[2] prompts carry the new rules")
    pr = PromptRenderer(schema_loader=sl)
    md = pr.render_team_extractor_prompt(team_name="MetadataTeam", team_prompt_template="metadata_team.j2",
        schema_section="report_metadata", pipeline_version="v2",
        available_tools=[ToolDescriptor("date_parser", "x", {})], parser_hypothesis_count=0)
    check("metadata prompt mentions Accession_Number + Signing_Pathologist",
          "Accession_Number" in md and "Signing_Pathologist" in md)
    tb = pr.render_team_extractor_prompt(team_name="TestedBiomarkerTeam", team_prompt_template="tested_biomarker_team.j2",
        schema_section="tested_biomarker_umbrella", pipeline_version="v2",
        available_tools=[ToolDescriptor("hgnc_normalize", "x", {})], parser_hypothesis_count=0)
    check("tested prompt mentions biomarkers_tested_no_result + QNS",
          "biomarkers_tested_no_result" in tb and "QNS" in tb)

    print("[3] scorer no longer penalizes extra report_metadata fields")
    gt = {"report_title": "X", "Patient_First_Name": "A"}
    actual = {"report_title": "X", "Patient_First_Name": "A", "Accession_Number": "SP25-1", "Report_Type": "Surgical Pathology"}
    rep = score_report_metadata(doc_id="t", expected=gt, actual=actual)
    check("extra fields not penalized → pass_rate 1.0", rep.pass_rate == 1.0, f"{rep.passed}/{rep.total}")

    print("[4] enriched specimen_demo scores all sections at F1 1.0 (vs itself)")
    env = json.load(open("ground_truth/specimen_demo.json", encoding="utf-8"))["genomic_pathology_extraction"]
    scores = {s.section: s for s in score_all_sections("specimen_demo", env, copy.deepcopy(env))}
    for sec in ("report_metadata", "other_molecular_biomarker_umbrella", "tested_biomarker_umbrella",
                "significant_findings", "clinical_information"):
        s = scores.get(sec)
        check(f"{sec} F1=1.0", s is not None and abs(s.f1 - 1.0) < 1e-6, "" if s else "section not scored")

    print("-" * 60)
    if fails:
        print(f"PRODPARITY VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("PRODPARITY VERIFY: PASS — production gaps closed + scored.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
