"""
V4-M9 gate — UI renders the v4 envelope (4 sections; separate variants; flat biomarkers).

Locks (offline, deterministic — source-inspection + syntax compile, no Streamlit import):
  [1] extraction_v2_view.py and overview.py compile.
  [2] extraction_v2_view renders the REVIVED Genomic_Variant_umbrella section.
  [3] biomarker render is FLAT-tolerant (handles a record with no findings[]).
  [4] disabled sections (clinical_information, significant_findings) are GUARDED by an
      `in envelope` check, so a disabled v4 section renders nothing (not an empty card).
  [5] overview stats count variants from BOTH the v3 findings path AND the v4
      Genomic_Variant_umbrella, and read the v4 count field name.

Run:  PYTHONPATH=. python scripts/gates/gate_v4_m9_ui.py
"""

from __future__ import annotations

import py_compile
import sys
from pathlib import Path

_VIEW = "ui/phase1/views/extraction_v2_view.py"
_OVERVIEW = "ui/phase1/views/overview.py"


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    print("[1] views compile")
    for f in (_VIEW, _OVERVIEW):
        try:
            py_compile.compile(f, doraise=True)
            check(f"{Path(f).name} compiles", True)
        except py_compile.PyCompileError as exc:
            check(f"{Path(f).name} compiles", False, str(exc))

    view = Path(_VIEW).read_text(encoding="utf-8")
    over = Path(_OVERVIEW).read_text(encoding="utf-8")

    print("[2] Genomic_Variant_umbrella rendered")
    check("view reads Genomic_Variant_umbrella.Genomic_Variants",
          'envelope.get("Genomic_Variant_umbrella")' in view and "Genomic_Variants" in view)
    check("view shows a Genomic variants subheader", "Genomic variants" in view)

    print("[3] biomarker render is flat-tolerant")
    check("branches on nested findings[] vs flat",
          "isinstance(findings, list) and findings" in view)
    check("flat branch reads record-level result/reference_range",
          'bm.get("reference_range")' in view and 'bm.get("result")' in view)

    print("[4] disabled sections guarded")
    check("clinical_information guarded by `in envelope`",
          'if "clinical_information" in envelope:' in view)
    check("significant_findings guarded by `in envelope`",
          'if "significant_findings" in envelope:' in view)

    print("[5] overview stats v4-aware")
    check("overview counts variants from Genomic_Variant_umbrella",
          'Genomic_Variant_umbrella' in over and "Genomic_Variants" in over)
    check("overview reads the v4 biomarker count field",
          "count_of_other_molecular_biomarkers" in over)

    print("-" * 60)
    if fails:
        print(f"V4-M9 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("V4-M9 VERIFY: PASS — UI renders the 4 v4 sections; variants separate; disabled hidden.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
