"""
Phase 2 M1 verification gate.

Done-when for M1:
  1. SchemaLoader builds a Pydantic model for EVERY v3 section (incl. the
     array + deeply-nested ones) and a sample instance validates.
  2. PromptRenderer renders every team Extractor prompt (metadata + the 4 new
     2a teams) + the Coverage Auditor prompt + the Block Profiler prompt with
     NO StrictUndefined / template errors.
  3. The Block Profiler closed vocab includes the new `addendum` role.
  4. v2 still loads (back-compat — Phase 1 scoring must not regress).

Run:  python scripts/gates/gate_p2_m1_sections_prompts.py
Exits non-zero on any failure.
"""

from __future__ import annotations

import sys

from core.prompt_renderer import PromptRenderer, ToolDescriptor
from core.schema_loader import SchemaLoader

V3 = "config/schemas/genomic_pathology_v3.json"
V2 = "config/schemas/genomic_pathology_v2.json"

# (team_prompt_template, schema_section)
TEAM_PROMPTS = [
    ("metadata_team.j2", "report_metadata"),
    ("molecular_biomarker_team.j2", "other_molecular_biomarker_umbrella"),
    ("tested_biomarker_team.j2", "tested_biomarker_umbrella"),
    ("clinical_info_team.j2", "clinical_information"),
]

# Minimal valid sample per section (only required keys; covers the nesting).
SAMPLES: dict[str, dict] = {
    "report_metadata": {
        "total_pages": 1, "report_title": None, "llm_confidence_score": 0.9,
        "Patient_MRN": None, "Patient_First_Name": None, "Patient_Last_Name": None,
        "Patient_DOB": None, "Vendor_Name": None, "Collection_Date": None,
        "Received_Date": None, "Report_Date": None, "Test_Name": None,
        "Procedure": None, "Ordering_Provider_First_Name": None,
        "Ordering_Provider_Last_Name": None, "Ordering_Provider_NPI": None,
        "Ordering_Provider_Title": None, "Practice_Name": None, "Practice_NPI": None,
        "Practice_Street_Address_1": None, "Practice_Street_Address_2": None,
        "Practice_City": None, "Practice_State": None, "Practice_ZIP_5digit": None,
        "Practice_ZIP_4digit": None,
    },
    "other_molecular_biomarker_umbrella": {
        "count": 2, "llm_confidence_score": 0.9,
        "other_molecular_biomarkers": [
            {
                "biomarker_name": "HER2", "specimen_id": None, "page_number": 3,
                "findings": [
                    {"method": "IHC", "result": "2+", "reference_range": None,
                     "details": None, "interpretation": "Equivocal", "assertion": "affirmed",
                     "method_source_type": "explicit", "page_number": 3, "variant_detail": None,
                     "occurrences": [{"block_id": "b12", "page": 3, "char_start": 0,
                                      "char_end": 7, "surface": "HER2 2+", "superseded": False}]},
                    {"method": "FISH", "result": "Amplified", "reference_range": None,
                     "details": None, "interpretation": None, "assertion": "affirmed",
                     "method_source_type": "explicit", "page_number": 7, "variant_detail": None,
                     "occurrences": []},
                ],
            },
            {
                # v3 merge: a gene sequence variant is a biomarker with variant_detail.
                "biomarker_name": "JAK2", "specimen_id": None, "page_number": 2,
                "findings": [
                    {"method": "PCR", "result": "Detected", "reference_range": None,
                     "details": None, "interpretation": None, "assertion": "affirmed",
                     "method_source_type": "explicit", "page_number": 2,
                     "occurrences": [{"block_id": "b9", "page": 2, "char_start": 0,
                                      "char_end": 4, "surface": "JAK2", "superseded": False}],
                     "variant_detail": {
                         "gene_symbol": "JAK2", "variant_allele_frequency": "42%",
                         "dna_change_type": "SNV", "amino_acid_change_type": "Missense",
                         "genomic_dna_change": None, "genomic_reference_sequence": None,
                         "coding_dna_change": "c.1849G>T", "transcript_reference_sequence": "NM_004972.4",
                         "amino_acid_change": "p.V617F", "amino_acid_reference_sequence": None,
                         "clinical_significance": "Pathogenic", "genomic_source_class": "Somatic",
                         "human_reference_sequence_assembly_version": "GRCh38",
                         "allelic_state": "Heterozygous", "chromosome_identifier": "9",
                         "genomic_position": None, "exon": "14",
                         "hgvs_normalized": {"coding": "c.1849G>T", "amino_acid": "p.V617F",
                                             "genomic": None, "valid": True, "version": 4, "backend": "parse"}}},
                ],
            },
        ],
    },
    "tested_biomarker_umbrella": {
        "count_of_tested_biomarkers": 2, "page_numbers": [4],
        "llm_confidence_score": 0.9, "tested_biomarkers": ["BRAF", "EGFR"],
    },
    "clinical_information": {
        "page_number": 1, "llm_confidence_score": 0.8,
        "number_of_clinical_histories": 1,
        "clinical_history": [{"pre_operative_diagnosis": "Suspected MDS"}],
        "reason_for_study": "Rule out JAK2", "clinical_finding_details": None,
    },
    "significant_findings": {
        "count_of_specimen_findings": 1, "llm_confidence_score": 0.8,
        "specimen_findings": [{
            "specimen": [{"specimen_id": "A", "tissue_type": "Breast",
                          "description": None, "tissue_site": "Right breast",
                          "laterality": "Right", "page_number": 1,
                          "llm_confidence_score": 0.9}],
            "procedure_details": {"procedure": "Core Biopsy", "specimen_count": 1,
                                  "page_number": 1, "llm_confidence_score": 0.9},
            "gross_description": {"text": "...", "page_number": 1, "llm_confidence_score": 0.9},
            "microscopic_description": {"text": "...", "page_number": 2, "llm_confidence_score": 0.9},
            "histologic_findings": [{"finding": "IDC", "interpretation": None,
                                     "page_number": 2, "llm_confidence_score": 0.9}],
            "ancillary_studies_findings": [],
            "morphology_findings": {"nuclear_grade": "3", "growth_pattern": None,
                                    "size": "2.1 cm", "necrosis": None,
                                    "calcifications": None, "tumor_extent": None},
            "pTNM_staging_details": {"pTNM_stage": "pT2N1",
                                     "staging_system_version": "AJCC 8th Edition",
                                     "page_number": 2, "llm_confidence_score": 0.9},
            "lymph_node_details": {"lymph_node_status": "Positive",
                                   "number_of_lymph_nodes_examined": 14,
                                   "number_of_lymph_nodes_positive": 2, "text": "...",
                                   "page_number": 2, "llm_confidence_score": 0.9},
        }],
    },
}


def main() -> int:
    failures: list[str] = []

    # ---- 1. v3 loads; every section builds a model + a sample validates -----
    sl = SchemaLoader.from_path(V3)
    sections = sl.list_sections()
    print(f"[1] v3 sections ({len(sections)}): {sections}")
    for s in sections:
        try:
            sec = sl.get_section(s)
            assert sec.pydantic_model is not None
            sl.validate(s, SAMPLES[s])
            print(f"    OK  {s:<38} model={sec.pydantic_model.__name__} "
                  f"fields={len(sec.fields)}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"section {s}: {exc}")
            print(f"    FAIL {s}: {exc}")

    # ---- 2. all prompts render (StrictUndefined catches missing vars) -------
    renderer = PromptRenderer(schema_loader=sl)
    sample_tools = [ToolDescriptor("hgnc_normalize", "Canonicalize a gene symbol.", {})]
    for template, section in TEAM_PROMPTS:
        try:
            text = renderer.render_team_extractor_prompt(
                team_name=f"{section}Team",
                team_prompt_template=template,
                schema_section=section,
                pipeline_version="v2",
                available_tools=sample_tools,
                parser_hypothesis_count=7,
            )
            assert section in text and "Field contract" in text
            print(f"[2] OK  extractor prompt rendered: {template} ({len(text)} chars)")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"render {template}: {exc}")
            print(f"[2] FAIL render {template}: {exc}")

    # Coverage Auditor (new parser-hypothesis shape with occurrences[])
    try:
        ca = renderer.render_coverage_auditor_prompt(
            team_name="MolecularBiomarkerTeam",
            schema_section="other_molecular_biomarker_umbrella",
            extractor_output=SAMPLES["other_molecular_biomarker_umbrella"],
            parser_hypothesis=[{
                "text": "JAK2", "target_umbrella": "other_molecular_biomarker_umbrella",
                "target_field_hint": None,
                "occurrences": [{"block_id": "b9", "page": 2, "char_start": 0,
                                 "char_end": 4, "text_role": "results_table",
                                 "evidence_excerpt": "JAK2 V617F ..."}],
                "pages": [2], "source_models": ["en_ner_bionlp13cg_md"],
                "confidence": 0.8, "rationale": "gene mention",
            }],
            coverage_gap_tolerance=0.10,
        )
        assert "occurrences" in ca
        print(f"[2] OK  coverage_auditor prompt rendered ({len(ca)} chars)")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"render coverage_auditor: {exc}")
        print(f"[2] FAIL render coverage_auditor: {exc}")

    # Block Profiler (sample addendum block)
    try:
        bp = renderer.render_block_profiler_prompt(
            doc_id="demo", total_pages=1,
            blocks=[{"block_id": "b1", "page_number": 1, "bbox": [0, 0, 1, 1],
                     "section_path": "page_1", "text": "Addendum: HER2 amended to negative."}],
        )
        assert "addendum" in bp, "block_profiler vocab missing 'addendum'"
        print(f"[2] OK  block_profiler prompt rendered, 'addendum' role present "
              f"({len(bp)} chars)")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"render block_profiler: {exc}")
        print(f"[2] FAIL render block_profiler: {exc}")

    # ---- 3. block profiler code vocab includes addendum ---------------------
    from typing import get_args
    from preprocess.block_profiler import TextRole, TargetUmbrellaHint
    if "addendum" in get_args(TextRole):
        print("[3] OK  TextRole includes 'addendum'")
    else:
        failures.append("TextRole missing 'addendum'")
    if {"significant_findings", "clinical_information"} <= set(get_args(TargetUmbrellaHint)):
        print("[3] OK  TargetUmbrellaHint includes the 2 v3 sections")
    else:
        failures.append("TargetUmbrellaHint missing v3 sections")

    # ---- 4. v2 back-compat ---------------------------------------------------
    try:
        v2 = SchemaLoader.from_path(V2)
        assert v2.list_sections() == [
            "report_metadata", "Genomic_Variant_umbrella",
            "other_molecular_biomarker_umbrella", "tested_biomarker_umbrella",
        ]
        print("[4] OK  v2 still loads with its 4 sections")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"v2 back-compat: {exc}")
        print(f"[4] FAIL v2 back-compat: {exc}")

    print("-" * 60)
    if failures:
        print(f"M1 VERIFY: FAIL ({len(failures)} issue(s))")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("M1 VERIFY: PASS — all sections build + validate, all prompts render.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
