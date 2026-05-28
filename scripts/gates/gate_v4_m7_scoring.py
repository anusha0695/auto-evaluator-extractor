"""
V4-M7 gate — scorer covers the 4 v4 sections + v4 ground truth is valid (D1/D2/D3).

Locks (offline, deterministic):
  [1] ground_truth/demo_v4.json is structurally VALID against genomic_pathology_v4.json
      (4 required sections present; the JAK2 V617F variant lives in Genomic_Variant_umbrella;
      other_molecular_biomarkers is empty; significant_findings/clinical_information OMITTED).
  [2] The scorer scores Genomic_Variant_umbrella: self-scoring demo_v4 against itself yields
      F1==1.0 with one matched variant, and a high field_pass_rate over the verbatim fields.
  [3] Disabled sections are NOT scored for v4 (significant_findings + clinical_information
      absent from the GT → no SectionScore for them).
  [4] v3-safety: self-scoring the v3 demo.json still produces NO Genomic_Variant_umbrella
      score (additive block skipped) and DOES score other_molecular_biomarker_umbrella —
      so v2/v3 scoring is unchanged.
  [5] _VARIANT_SCORE_FIELDS is defined and the flat-biomarker score set gained reference_range.

Run:  PYTHONPATH=. python scripts/gates/gate_v4_m7_scoring.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from scripts.score_against_ground_truth import _VARIANT_SCORE_FIELDS, score_all_sections

_V4_SCHEMA = "config/schemas/genomic_pathology_v4.json"
_GT_V4 = "ground_truth/demo_v4.json"
_GT_V3 = "ground_truth/demo.json"
_SCORER = "scripts/score_against_ground_truth.py"


def _env(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))["genomic_pathology_extraction"]


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    print("[1] demo_v4.json is valid against the v4 schema")
    gt4 = _env(_GT_V4)
    check("4 required sections present",
          all(k in gt4 for k in ("count_of_extracted_objects", "report_metadata",
                                 "Genomic_Variant_umbrella", "other_molecular_biomarker_umbrella",
                                 "tested_biomarker_umbrella")))
    check("JAK2 V617F lives in Genomic_Variant_umbrella",
          gt4["Genomic_Variant_umbrella"]["Genomic_Variants"][0]["gene_studied"] == "JAK2" and
          gt4["Genomic_Variant_umbrella"]["Genomic_Variants"][0]["amino_acid_change"] == "V617F")
    check("other_molecular_biomarkers is empty (variant moved out)",
          gt4["other_molecular_biomarker_umbrella"]["other_molecular_biomarkers"] == [])
    check("disabled sections OMITTED from GT",
          "significant_findings" not in gt4 and "clinical_information" not in gt4)
    try:
        from core.schema_loader import SchemaLoader
        from verification.schema_validator import SchemaValidator
        loader = SchemaLoader.from_path(_V4_SCHEMA)
        # v4 disables significant_findings + clinical_information (D3) — the validator
        # skips them so a legitimately-absent disabled section is not a structural error.
        passed, errors = SchemaValidator(schema_loader=loader).validate_envelope(
            gt4, disabled_sections={"significant_findings", "clinical_information"})
        structural = [e for e in errors if str(e.get("type", "")) not in
                      {"pattern", "format", "minimum", "maximum", "minLength", "maxLength",
                       "exclusiveMinimum", "exclusiveMaximum"}]
        check("schema validator: no structural errors", not structural, str(structural[:3]))
    except Exception as exc:  # noqa: BLE001
        check("schema validator ran", False, repr(exc))

    print("[2] scorer scores Genomic_Variant_umbrella (self-score == perfect)")
    scores4 = {s.section: s for s in score_all_sections("demo", gt4, gt4)}
    gv = scores4.get("Genomic_Variant_umbrella")
    check("Genomic_Variant_umbrella is scored", gv is not None)
    check("variant section F1 == 1.0 on self-score", bool(gv) and abs(gv.f1 - 1.0) < 1e-9,
          f"f1={gv.f1 if gv else None}")
    check("one variant matched", bool(gv) and gv.matched == 1 and gv.expected_count == 1)
    check("variant field_pass_rate == 1.0",
          bool(gv) and gv.field_pass_rate is not None and abs(gv.field_pass_rate - 1.0) < 1e-9,
          f"fpr={gv.field_pass_rate if gv else None}")
    check("report_metadata + tested also perfect on self-score",
          abs(scores4["report_metadata"].f1 - 1.0) < 1e-9 and
          abs(scores4["tested_biomarker_umbrella"].f1 - 1.0) < 1e-9)

    print("[3] disabled sections are NOT scored for v4")
    check("significant_findings not scored", "significant_findings" not in scores4)
    check("clinical_information not scored", "clinical_information" not in scores4)

    print("[4] v3-safety: v3 self-score unchanged (no variant section; biomarker scored)")
    gt3 = _env(_GT_V3)
    scores3 = {s.section: s for s in score_all_sections("demo", gt3, gt3)}
    check("v3 has NO Genomic_Variant_umbrella score (additive block skipped)",
          "Genomic_Variant_umbrella" not in scores3)
    check("v3 still scores other_molecular_biomarker_umbrella",
          "other_molecular_biomarker_umbrella" in scores3)
    check("v3 biomarker self-score still perfect",
          abs(scores3["other_molecular_biomarker_umbrella"].f1 - 1.0) < 1e-9)

    print("[5] scorer field sets")
    check("_VARIANT_SCORE_FIELDS defined + non-trivial",
          isinstance(_VARIANT_SCORE_FIELDS, list) and len(_VARIANT_SCORE_FIELDS) >= 6)
    src = Path(_SCORER).read_text(encoding="utf-8")
    check("flat-biomarker score set includes reference_range",
          '"reference_range", "biomarker_class"' in src)

    print("-" * 60)
    if fails:
        print(f"V4-M7 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("V4-M7 VERIFY: PASS — scorer covers the 4 v4 sections; v4 GT valid; v3 scoring intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
