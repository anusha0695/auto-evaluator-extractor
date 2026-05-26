"""
Phase 2 M10 verification gate — multi-section scoring (synthetic; no PHI).

Done-when (PHASE_2_PLAN.md M10): all 2a sections are scored — variant array
(identity-matched P/R + field accuracy), tested-biomarker panel (set P/R, alias
tolerant), other_molecular (v2-flat gt vs v3-findings[] output), clinical_information
(object). report_metadata scoring unchanged.

Run:  PYTHONPATH=. python scripts/gates/gate_p2_m10_scoring.py
"""

from __future__ import annotations

import sys

from scripts.score_against_ground_truth import score_all_sections


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    # v3 merge: sequence variants are biomarker findings with a nested
    # variant_detail. GT biomarkers = PD-L1 (IHC), JAK2 (variant), HER2 (variant).
    gt = {
        "report_metadata": {"report_title": "Molecular Genetics", "Patient_First_Name": "A",
                            "llm_confidence_score": 0.9},
        "tested_biomarker_umbrella": {"tested_biomarkers": ["JAK2", "HER2"]},
        "other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": [
            {"biomarker_name": "PD-L1", "method": "IHC", "result": "80%"},   # v2 flat
            {"biomarker_name": "JAK2", "findings": [{"method": "PCR", "result": "Detected",
                "variant_detail": {"amino_acid_change": "p.V617F", "coding_dna_change": "c.1849G>T",
                                   "genomic_source_class": "Somatic"}}]},
            {"biomarker_name": "HER2", "findings": [{"method": "FISH", "result": "Amplified",
                "variant_detail": {"amino_acid_change": "p.X"}}]}]},
        "clinical_information": {"reason_for_study": "Rule out JAK2",
                                 "clinical_finding_details": None, "number_of_clinical_histories": 1},
    }
    ex = {
        "report_metadata": {"report_title": "molecular genetics", "Patient_First_Name": "A",
                            "llm_confidence_score": 0.95},
        "tested_biomarker_umbrella": {"tested_biomarkers": ["JAK2", "ERBB2"]},   # ERBB2≡HER2 alias
        "other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": [
            {"biomarker_name": "PD-L1", "findings": [{"method": "IHC", "result": "80%"}]},  # v3 nested
            {"biomarker_name": "JAK2", "findings": [{"method": "PCR", "result": "Detected",
                "variant_detail": {"amino_acid_change": "p.V617F", "coding_dna_change": "c.1849G>T",
                                   "genomic_source_class": "Somatic"}}]}]},  # HER2 missing → FN
        "clinical_information": {"reason_for_study": "rule out JAK2",
                                 "clinical_finding_details": None, "number_of_clinical_histories": 1},
    }

    scores = {s.section: s for s in score_all_sections("synthetic", gt, ex)}
    print("  sections scored:", sorted(scores))
    assert "Genomic_Variant_umbrella" not in scores, "variant umbrella should be merged away"

    check("report_metadata F1=1.0", abs(scores["report_metadata"].f1 - 1.0) < 1e-6,
          f"{scores['report_metadata'].f1:.3f}")

    t = scores["tested_biomarker_umbrella"]
    check("panel alias match → F1=1.0 (HER2≡ERBB2)", abs(t.f1 - 1.0) < 1e-6,
          f"P={t.precision:.2f} R={t.recall:.2f}")

    # Biomarker umbrella now carries variants too: PD-L1 + JAK2 matched, HER2 (variant) missed → FN.
    b = scores["other_molecular_biomarker_umbrella"]
    check("biomarker P=1.0 R=0.667 (HER2 variant missed)",
          b.matched == 2 and abs(b.precision - 1.0) < 1e-6 and abs(b.recall - (2/3)) < 1e-6,
          f"matched={b.matched}/{b.expected_count} P={b.precision:.2f} R={b.recall:.2f}")
    check("variant_detail scored on matched biomarkers (field_pass_rate high)",
          (b.field_pass_rate or 0) >= 0.8, str(b.field_pass_rate))

    c = scores["clinical_information"]
    check("clinical_information fields all pass", abs(c.field_pass_rate - 1.0) < 1e-6, str(c.field_pass_rate))

    print("-" * 60)
    if fails:
        print(f"M10 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("M10 VERIFY: PASS — all 2a sections scored (set/array/alias/flatten/object).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
