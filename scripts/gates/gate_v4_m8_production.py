"""
V4-M8 gate — production mapping retargeted for v4 (D1/D2), v3 output byte-identical.

Locks (offline, deterministic; to_production is pure):
  [1] v3-safety: to_production on the v3 demo envelope still folds the JAK2 variant
      (nested findings[] + variant_detail) into pathology_biomarkers_findings with the
      HGVS in `details` — unchanged behavior.
  [2] v4 variant: to_production on the v4 demo envelope sources the SEPARATE
      Genomic_Variant_umbrella — JAK2 appears in pathology_biomarkers_findings with the
      HGVS folded into `details` and clinical_significance as interpretation; tested maps JAK2.
  [3] v4 FLAT biomarker: a flat other_molecular record (the record IS the finding, no
      findings[]) is emitted 1:1 (ER Positive) — not dropped.
  [4] production_label maps Genomic_Variant_umbrella refs into pathology_biomarkers_findings
      (gene_studied → biomarker_name; HGVS leaves → details (folded)).

Run:  PYTHONPATH=. python scripts/gates/gate_v4_m8_production.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from transform.to_production import production_label, to_production

_GT_V3 = "ground_truth/demo.json"
_GT_V4 = "ground_truth/demo_v4.json"


def _env(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))["genomic_pathology_extraction"]


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    print("[1] v3 output unchanged (variant folded from nested findings[])")
    p3 = to_production(_env(_GT_V3))["pathology_extraction"]
    bm3 = p3["pathology_biomarkers_findings"]["pathology_biomarkers"]
    jak3 = next((b for b in bm3 if b["biomarker_name"] == "JAK2"), None)
    check("v3 JAK2 biomarker present", jak3 is not None)
    check("v3 JAK2 details fold the HGVS (V617F)",
          bool(jak3) and "V617F" in (jak3["findings"][0]["details"] or ""),
          (jak3["findings"][0]["details"] if jak3 else ""))

    print("[2] v4 variant sourced from the separate Genomic_Variant_umbrella")
    p4 = to_production(_env(_GT_V4))["pathology_extraction"]
    bm4 = p4["pathology_biomarkers_findings"]["pathology_biomarkers"]
    jak4 = next((b for b in bm4 if b["biomarker_name"] == "JAK2"), None)
    check("v4 JAK2 biomarker present (from Genomic_Variant_umbrella)", jak4 is not None)
    check("v4 JAK2 details fold the HGVS (V617F + 1849G>T)",
          bool(jak4) and "V617F" in (jak4["findings"][0]["details"] or "")
          and "1849G>T" in (jak4["findings"][0]["details"] or ""),
          (jak4["findings"][0]["details"] if jak4 else ""))
    check("v4 JAK2 interpretation ← clinical_significance",
          bool(jak4) and "JAK2 tyrosine kinase" in (jak4["findings"][0]["interpretation"] or ""))
    check("v4 tested maps JAK2",
          any(x["biomarker_name"] == "JAK2" for x in
              p4["pathology_biomarkers_mentioned"]["biomarkers_tested_with_result"]["biomarkers_tested_with_result"]))

    print("[3] v4 FLAT biomarker emitted 1:1 (record is the finding)")
    flat_env = {
        "report_metadata": {},
        "Genomic_Variant_umbrella": {"Genomic_Variants": []},
        "other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": [
            {"biomarker_name": "ER", "method": "IHC", "result": "Positive",
             "reference_range": None, "interpretation": "positive", "page_number": 2},
            {"biomarker_name": "Ki-67", "method": "IHC", "result": None, "page_number": 2},  # no result → dropped
        ]},
        "tested_biomarker_umbrella": {},
    }
    pf = to_production(flat_env)["pathology_extraction"]["pathology_biomarkers_findings"]
    names = [b["biomarker_name"] for b in pf["pathology_biomarkers"]]
    check("flat ER (with result) emitted", "ER" in names, str(names))
    check("flat Ki-67 (no result) dropped — Decision C", "Ki-67" not in names)
    er = next((b for b in pf["pathology_biomarkers"] if b["biomarker_name"] == "ER"), None)
    check("flat ER finding carries result Positive",
          bool(er) and er["findings"][0]["result"] == "Positive")

    print("[4] production_label maps Genomic_Variant_umbrella refs")
    check("gene_studied → biomarker_name",
          production_label("Genomic_Variant_umbrella.Genomic_Variants[0].gene_studied", {})
          == "pathology_biomarkers_findings › biomarker_name")
    check("amino_acid_change → details (folded)",
          production_label("Genomic_Variant_umbrella.Genomic_Variants[0].amino_acid_change", {})
          == "pathology_biomarkers_findings › findings › details (folded)")
    check("page_number → None (dropped)",
          production_label("Genomic_Variant_umbrella.Genomic_Variants[0].page_number", {}) is None)

    print("-" * 60)
    if fails:
        print(f"V4-M8 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("V4-M8 VERIFY: PASS — production mapping retargeted (v4 flat + variant section); v3 output intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
