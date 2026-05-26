"""
Phase 2 M2 verification gate — normalization + negation tools.

Done-when (from PHASE_2_PLAN.md M2):
  - hgnc_normalize("JAK-2") == "JAK2" (exact) and fuzzy "JAKZ" → "JAK2" (flagged);
    ambiguous "XRAS" does NOT auto-pick.
  - method_normalize("Immunohistochemistry") == "IHC"; biomarker_normalize("CD274")=="PD-L1".
  - hgvs_validate("c.1849G>T") valid; transcript version preserved.
  - assertion tags "suggestive of" → uncertain; "Not Detected" → negated.
  - normalize_quantity: 12% ↔ 0.12 and 2.3 cm ↔ 23 mm equivalent; "O%" not repaired.
  - all 5 tools register in tool_registry (build_tools_for_state + descriptors_from_yaml)
    and invoke through the LangChain StructuredTool wrapper.

Run:  PYTHONPATH=. python scripts/gates/gate_p2_m2_normalization_tools.py
"""

from __future__ import annotations

import sys

from preprocess.hgnc_resolver import hgnc_normalize
from preprocess.hgvs_validate import hgvs_validate
from preprocess.negation import assess_assertion
from preprocess.normalizers import biomarker_normalize, method_normalize, normalize_quantity

NEW_TOOLS = ["hgnc_normalize", "hgvs_validate", "biomarker_normalize",
             "method_normalize", "normalize_quantity"]


def main() -> int:
    fails: list[str] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        status = "OK  " if cond else "FAIL"
        print(f"  [{status}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    print("[1] HGNC resolver (correct + flag + ambiguity policy)")
    r = hgnc_normalize("JAK-2"); check("JAK-2 → JAK2 exact", r["canonical"] == "JAK2" and not r["fuzzy"])
    r = hgnc_normalize("HER2"); check("HER2 → ERBB2 exact", r["canonical"] == "ERBB2")
    r = hgnc_normalize("JAKZ"); check("JAKZ → JAK2 fuzzy+flagged", r["canonical"] == "JAK2" and r["fuzzy"] and r["status"] == "fuzzy")
    r = hgnc_normalize("XRAS"); check("XRAS ambiguous → no pick", r["canonical"] is None and r["status"] == "ambiguous", str(r["candidates"]))
    r = hgnc_normalize("ZZZZ"); check("ZZZZ unknown", r["canonical"] is None and r["status"] == "unknown")

    print("[2] HGVS validate (offline, version-aware)")
    r = hgvs_validate("c.1849G>T"); check("c.1849G>T valid", r["valid"], r["backend"])
    r = hgvs_validate("NM_004972.4:c.1849G>T"); check("transcript version preserved", r["valid"] and r["version"] == 4, f"acc={r['accession']} ver={r['version']}")
    r = hgvs_validate("totally not hgvs"); check("junk rejected", not r["valid"])

    print("[3] Assertion / negation")
    check("'suggestive of' → uncertain", assess_assertion("findings suggestive of MDS")["assertion"] == "uncertain")
    check("'Not Detected' → negated", assess_assertion("JAK2 V617F Not Detected")["assertion"] == "negated")
    check("clear positive → affirmed", assess_assertion("JAK2 V617F detected")["assertion"] == "affirmed")

    print("[4] Synonym normalizers")
    check("Immunohistochemistry → IHC", method_normalize("Immunohistochemistry")["canonical"] == "IHC")
    check("CD274 → PD-L1", biomarker_normalize("CD274")["canonical"] == "PD-L1")

    print("[5] Quantity equivalence (verbatim never repaired)")
    check("12% ↔ 0.12", normalize_quantity("12%")["canonical_value"] == normalize_quantity("0.12")["canonical_value"])
    check("2.3 cm ↔ 23 mm", normalize_quantity("2.3 cm")["canonical_value"] == normalize_quantity("23 mm")["canonical_value"])
    check("O% not repaired", normalize_quantity("O%")["canonical_value"] is None)

    print("[6] Registry wiring (StructuredTool invoke + yaml descriptors)")
    from core.tool_registry import build_tools_for_state, descriptors_from_yaml
    tools = build_tools_for_state(state={}, allowlist=NEW_TOOLS)
    by_name = {t.name: t for t in tools}
    check("all 5 tools built", set(by_name) == set(NEW_TOOLS), str(sorted(by_name)))
    try:
        inv = by_name["hgnc_normalize"].invoke({"symbol": "HER2"})
        check("hgnc_normalize tool invoke", inv.get("canonical") == "ERBB2")
        inv = by_name["normalize_quantity"].invoke({"value": "12%"})
        check("normalize_quantity tool invoke", inv.get("canonical_value") == 0.12)
    except Exception as exc:  # noqa: BLE001
        check("tool invoke", False, str(exc))
    desc_names = {d.name for d in descriptors_from_yaml(allowlist=NEW_TOOLS)}
    check("all 5 tools in tools.yaml", set(NEW_TOOLS) <= desc_names, str(sorted(desc_names)))

    print("-" * 60)
    if fails:
        print(f"M2 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("M2 VERIFY: PASS — all normalization/negation tools correct + registered.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
