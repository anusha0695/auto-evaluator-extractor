"""
V4-M12 gate — config-driven cross-section DEDUP (the canonical reconciliation layer).

Architectural intent: extraction is recall-first. NER + team prompts may legitimately
route the same entity to multiple sections (gene name as variant AND on the panel AND
as a flat biomarker line). Dedup applies the schema's section-ownership policy
DOWNSTREAM via HGNC-canonical matching — declared in `config/dedup_policy.yaml`, no
Python edits to add a new rule.

Locks (offline, deterministic):
  [1] config/dedup_policy.yaml exists and has a Genomic_Variant owns →
      drop_from other_molecular_biomarker rule with match_on: gene_key and the v3-safe
      `also_requires_filter_on_drop_from: findings[*].variant_detail`.
  [2] Linker drops a duplicate variant biomarker row when the same gene exists in
      Genomic_Variant_umbrella, AND preserves non-variant biomarkers (plain IHC).
  [3] HGNC canonicalisation is forgiving of trailing context — a biomarker_name
      `"JAK2 V617F Mutation"` still matches the variant `gene_studied: "JAK2"`.
  [4] v3 safety: when Genomic_Variant_umbrella is absent (v3 envelope), the rule is
      a no-op and no biomarker records are dropped.
  [5] The scalar_keys config (page_number) round-trips: extractor._coerce_scalar_tics
      reads from section_layout.yaml, not a hardcoded Python set.

Run:  PYTHONPATH=. python scripts/gates/gate_v4_m12_dedup_policy.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from agents.linker import Linker, _DEDUP_POLICY, _SECTION_LAYOUT
from agents.link_registry import LinkRegistry


def _v4_envelope_with_duplicate():
    return {
        "report_metadata": {},
        "Genomic_Variant_umbrella": {
            "count_of_Genomic_Variants": 1, "llm_confidence_score": None,
            "Genomic_Variants": [{"gene_studied": "JAK2", "amino_acid_change": "V617F"}],
        },
        "other_molecular_biomarker_umbrella": {
            "count_of_other_molecular_biomarkers": 2, "llm_confidence_score": None,
            "other_molecular_biomarkers": [
                # duplicate of variant — biomarker_name has extra context ("JAK2 V617F Mutation")
                # AND carries variant_detail (so the v3-safety filter targets it).
                {"biomarker_name": "JAK2 V617F Mutation", "method": "PCR", "result": "Not Detected",
                 "findings": [{"variant_detail": {"amino_acid_change": "V617F"}}]},
                {"biomarker_name": "ER", "method": "IHC", "result": "Positive"},  # plain IHC — KEEP
            ],
        },
        "tested_biomarker_umbrella": {
            "count_of_tested_biomarkers": 1, "page_numbers": [1], "llm_confidence_score": None,
            "tested_biomarkers": ["JAK2"],
        },
    }


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    print("[1] config/dedup_policy.yaml is well-formed")
    check("at least one rule loaded", bool(_DEDUP_POLICY), str(len(_DEDUP_POLICY)))
    rule = next((r for r in _DEDUP_POLICY
                 if r.get("when_present_in") == "Genomic_Variant_umbrella"
                 and r.get("drop_from") == "other_molecular_biomarker_umbrella"), None)
    check("Genomic_Variant owns → drop_from other_molecular_biomarker", rule is not None)
    check("match_on: gene_key", bool(rule) and rule.get("match_on") == "gene_key")
    check("v3-safety filter is `findings[*].variant_detail`",
          bool(rule) and "variant_detail" in (rule.get("also_requires_filter_on_drop_from") or ""))

    print("[2] dedup drops the duplicate variant biomarker, preserves plain IHC")
    env4 = _v4_envelope_with_duplicate()
    reg = LinkRegistry.from_path("config/link_registry_v4.yaml", max_tier=2)
    res = Linker(link_registry=reg).link(sections=env4, blocks=[], block_profiles=[])
    bms = (res.envelope["other_molecular_biomarker_umbrella"] or {}).get("other_molecular_biomarkers") or []
    names = [b.get("biomarker_name") for b in bms]
    check("`JAK2 V617F Mutation` duplicate REMOVED", "JAK2 V617F Mutation" not in names, str(names))
    check("plain IHC `ER` PRESERVED", "ER" in names, str(names))
    check("notes records the dedup", "dedup -1" in (res.notes or ""), repr(res.notes))
    check("count counts 1 variant + 1 biomarker + 1 tested = 3",
          res.envelope["count_of_extracted_objects"] == 3, str(res.envelope["count_of_extracted_objects"]))
    # And the gene-key seed still fires under the v4 registry.
    vop = [(L.from_ref, L.to_ref) for L in res.links if L.type == "variant_on_panel"]
    check("variant_on_panel link emitted JAK2-variant ↔ JAK2-panel", len(vop) == 1, str(vop))

    print("[3] HGNC canon is forgiving of trailing context (biomarker_name → gene)")
    lk = Linker(link_registry=reg)
    check("`JAK2 V617F Mutation` canonicalises to JAK2",
          (lk._canon("JAK2 V617F Mutation") or "").upper() == "JAK2",
          str(lk._canon("JAK2 V617F Mutation")))
    check("bare `JAK2` still canonicalises to JAK2",
          (lk._canon("JAK2") or "").upper() == "JAK2")

    print("[4] v3 envelope is a no-op (variant section absent)")
    v3_env = {
        "report_metadata": {},
        "other_molecular_biomarker_umbrella": {
            "count_of_other_molecular_biomarkers": 1, "llm_confidence_score": None,
            "other_molecular_biomarkers": [
                {"biomarker_name": "JAK2", "method": "PCR", "result": "Not Detected",
                 "findings": [{"variant_detail": {"amino_acid_change": "V617F"}}]},
            ],
        },
        "tested_biomarker_umbrella": {
            "count_of_tested_biomarkers": 1, "page_numbers": [1], "llm_confidence_score": None,
            "tested_biomarkers": ["JAK2"],
        },
    }
    res3 = Linker().link(sections=v3_env, blocks=[], block_profiles=[])
    bms3 = (res3.envelope["other_molecular_biomarker_umbrella"] or {}).get("other_molecular_biomarkers") or []
    check("v3 biomarker JAK2 NOT dropped (Genomic_Variant_umbrella absent → rule no-ops)",
          len(bms3) == 1 and bms3[0].get("biomarker_name") == "JAK2", str([b.get("biomarker_name") for b in bms3]))

    print("[5] scalar_keys is config-driven (not a hardcoded Python set)")
    yml = yaml.safe_load(Path("config/section_layout.yaml").read_text(encoding="utf-8")) or {}
    scalar_keys = yml.get("scalar_keys") or []
    check("section_layout.yaml declares scalar_keys", "page_number" in scalar_keys, str(scalar_keys))
    # round-trip via extractor's coercion (uses section_layout's set)
    from agents.extractor import _coerce_scalar_tics
    obj = {"page_number": [1], "nested": [{"page_number": []}], "page_numbers": [1, 2]}
    _coerce_scalar_tics(obj)
    check("coerce unwraps page_number: [1] → 1", obj["page_number"] == 1)
    check("coerce turns page_number: [] → None", obj["nested"][0]["page_number"] is None)
    check("coerce leaves plural page_numbers alone", obj["page_numbers"] == [1, 2])

    print("-" * 60)
    if fails:
        print(f"V4-M12 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("V4-M12 VERIFY: PASS — dedup is the canonical correctness layer; extraction stays recall-first.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
