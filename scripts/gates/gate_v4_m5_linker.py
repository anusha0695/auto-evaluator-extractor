"""
V4-M5 gate — link registry retargeted for the 4 active v4 sections (D1/D2/D3).

Locks (offline, deterministic):
  [1] config/link_registry_v4.yaml exists and is well-formed; v3 link_registry.yaml
      is UNTOUCHED (still carries the significant_findings family) — non-destructive (D5).
  [2] v4 registry active types are exactly the 4 retargeted links, and every one of
      them ONLY touches the 4 enabled v4 sections (Genomic_Variant_umbrella,
      other_molecular_biomarker_umbrella, tested_biomarker_umbrella). NO link touches
      a DISABLED section (significant_findings / clinical_information).
  [3] variant_on_panel re-points its variant endpoint to Genomic_Variant_umbrella
      (was other_molecular_biomarker_umbrella in v3 — the revived-variant retarget).
  [4] validate_against_schema(v4 registry, v4 schema_loader) == clean (every endpoint
      names a real v4 schema section).
  [5] runner v4 branch binds link_registry_v4.yaml; build_graph_dependencies accepts
      link_registry_path and threads it into LinkRegistry.from_path.

Run:  PYTHONPATH=. python scripts/gates/gate_v4_m5_linker.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from agents.link_registry import LinkRegistry, validate_against_schema

_V4_REG = "config/link_registry_v4.yaml"
_V3_REG = "config/link_registry.yaml"
_V4_SCHEMA = "config/schemas/genomic_pathology_v4.json"
_GRAPH_LINEAR = "pipeline/graph_linear.py"
_RUNNER = "pipeline/runner.py"

_V4_ENABLED_SECTIONS = {
    "report_metadata",
    "Genomic_Variant_umbrella",
    "other_molecular_biomarker_umbrella",
    "tested_biomarker_umbrella",
}
_V4_DISABLED_SECTIONS = {"significant_findings", "clinical_information"}
_EXPECTED_ACTIVE = {"tested_to_result", "variant_on_panel", "variant_superseded_by", "superseded_by"}


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    print("[1] v4 registry exists; v3 registry untouched (non-destructive)")
    check("config/link_registry_v4.yaml exists", Path(_V4_REG).exists())
    v3 = yaml.safe_load(Path(_V3_REG).read_text(encoding="utf-8"))
    v3_secs = {r["from_section"] for r in v3["link_types"]} | {r["to_section"] for r in v3["link_types"]}
    check("v3 registry still carries significant_findings (untouched)", "significant_findings" in v3_secs)

    print("[2] v4 active types touch ONLY enabled sections")
    reg = LinkRegistry.from_path(_V4_REG, max_tier=2)
    active = reg.active_types()
    active_names = {t.type for t in active}
    check("active types == the 4 retargeted v4 links", active_names == _EXPECTED_ACTIVE, str(sorted(active_names)))
    touched = {t.from_section for t in active} | {t.to_section for t in active}
    check("no active link touches a DISABLED section",
          not (touched & _V4_DISABLED_SECTIONS), str(sorted(touched & _V4_DISABLED_SECTIONS)))
    check("every endpoint is an enabled v4 section",
          touched <= _V4_ENABLED_SECTIONS, str(sorted(touched - _V4_ENABLED_SECTIONS)))

    print("[3] variant_on_panel re-points to the revived Genomic_Variant_umbrella")
    vop = reg.get("variant_on_panel")
    check("variant_on_panel exists + active", bool(vop and vop.active))
    check("variant_on_panel endpoints = {Genomic_Variant_umbrella, tested_biomarker_umbrella}",
          bool(vop) and vop.endpoints == frozenset({"Genomic_Variant_umbrella", "tested_biomarker_umbrella"}),
          str(sorted(vop.endpoints)) if vop else "missing")
    check("variant endpoint is NOT other_molecular_biomarker_umbrella (v3 shape dropped)",
          bool(vop) and "other_molecular_biomarker_umbrella" not in vop.endpoints)

    print("[4] validate_against_schema(v4) is clean")
    try:
        from core.schema_loader import SchemaLoader
        loader = SchemaLoader.from_path(_V4_SCHEMA)
        issues = validate_against_schema(_V4_REG, loader)
    except Exception as exc:  # noqa: BLE001 — schema_loader import is heavy; fall back to schema-key check
        print(f"    (schema_loader unavailable: {exc!r} — falling back to raw section-key check)")
        import json
        props = set(json.loads(Path(_V4_SCHEMA).read_text(encoding="utf-8"))["properties"])
        issues = []
        raw = yaml.safe_load(Path(_V4_REG).read_text(encoding="utf-8"))
        for r in raw["link_types"]:
            for side in ("from_section", "to_section"):
                if r[side] not in props:
                    issues.append(f"{r['type']} → unknown section {r[side]!r} ({side})")
    check("no registered endpoint names a non-existent v4 section", issues == [], str(issues))

    print("[4b] linker assembly is now fully GENERIC (driven by sections.keys() + section_layout.yaml)")
    from agents.linker import Linker
    _sections = {
        "report_metadata": {"total_pages": 1},
        "Genomic_Variant_umbrella": {"count_of_Genomic_Variants": 1, "llm_confidence_score": None,
                                     "Genomic_Variants": [{"gene_studied": "JAK2"}]},
        "other_molecular_biomarker_umbrella": {"count_of_other_molecular_biomarkers": 0,
                                               "llm_confidence_score": None, "other_molecular_biomarkers": []},
        "tested_biomarker_umbrella": {"count_of_tested_biomarkers": 1, "page_numbers": [1],
                                      "llm_confidence_score": None, "tested_biomarkers": ["JAK2"]},
    }
    env = Linker().link(sections=_sections, blocks=[], block_profiles=[]).envelope
    check("envelope emits EXACTLY the sections the active teams produced",
          set(env) - {"count_of_extracted_objects"} == set(_sections))
    check("envelope includes the revived Genomic_Variant_umbrella", "Genomic_Variant_umbrella" in env)
    check("envelope OMITS disabled significant_findings + clinical_information",
          "significant_findings" not in env and "clinical_information" not in env)
    check("count counts the variant via section_layout.yaml (1 variant + 1 tested = 2)",
          env.get("count_of_extracted_objects") == 2, str(env.get("count_of_extracted_objects")))
    # Lock: with the v4 link registry bound, the gene-key seed fires on the revived
    # variant section. (Default `Linker()` lazy-loads the v3 registry — for the v4
    # gene-key check we explicitly bind the v4 registry so the type vocabulary matches.)
    res = LinkRegistry.from_path(_V4_REG, max_tier=2)
    res = Linker(link_registry=res).link(sections=_sections, blocks=[], block_profiles=[])
    vop = [L for L in res.links if L.type == "variant_on_panel"]
    check("v4 gene-key seed emits variant_on_panel (Genomic_Variants ↔ tested) for JAK2",
          len(vop) == 1 and
          vop[0].from_ref.startswith("Genomic_Variant_umbrella.Genomic_Variants[") and
          vop[0].to_ref.startswith("tested_biomarker_umbrella.tested_biomarkers[") and
          "JAK2" in (vop[0].rationale or ""),
          str([(L.type, L.from_ref, L.to_ref) for L in res.links]))

    print("[5] run path binds the v4 registry")
    gl = Path(_GRAPH_LINEAR).read_text(encoding="utf-8")
    check("build_graph_dependencies accepts link_registry_path", "link_registry_path" in gl)
    check("LinkRegistry.from_path uses the param (not a hardcoded v3 path)",
          "LinkRegistry.from_path(link_registry_path" in gl)
    rn = Path(_RUNNER).read_text(encoding="utf-8")
    check("runner v4 branch binds link_registry_v4.yaml", "link_registry_v4.yaml" in rn)

    print("-" * 60)
    if fails:
        print(f"V4-M5 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("V4-M5 VERIFY: PASS — link registry retargeted to the 4 v4 sections; v3 registry intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
