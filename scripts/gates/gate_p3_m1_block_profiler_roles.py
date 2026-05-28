"""
Phase 3 M1 gate — Block-Profiler narrative roles + v3 routing vocab.

Checks:
  1. TextRole gained the 5 surgical/clinical roles.
  2. The block_profiler prompt renders and lists those roles.
  3. The prompt's target_umbrella_hints vocab includes significant_findings,
     clinical_information, and (v4 re-add) Genomic_Variant_umbrella.

Run:  PYTHONPATH=. python scripts/gates/gate_p3_m1_block_profiler_roles.py
"""

from __future__ import annotations

import sys
from typing import get_args

from core.prompt_renderer import PromptRenderer
from core.schema_loader import SchemaLoader
from preprocess.block_profiler import TextRole


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    new_roles = {"final_diagnosis", "gross_description", "microscopic_description",
                 "synoptic_report", "clinical_history"}

    print("[1] TextRole gained the narrative roles")
    roles = set(get_args(TextRole))
    for r in sorted(new_roles):
        check(f"TextRole has {r}", r in roles)

    print("[2] block_profiler prompt renders + lists the new roles")
    sl = SchemaLoader.from_path("config/schemas/genomic_pathology_v3.json")
    pr = PromptRenderer(schema_loader=sl)
    bp = pr.render_block_profiler_prompt(
        doc_id="t", total_pages=1,
        blocks=[{"block_id": "b1", "page_number": 1, "bbox": [0, 0, 1, 1],
                 "section_path": "page_1", "text": "Final Diagnosis: Invasive ductal carcinoma."}],
    )
    for r in sorted(new_roles):
        check(f"prompt mentions {r}", r in bp)

    print("[3] target_umbrella_hints vocab")
    check("prompt offers significant_findings", "significant_findings" in bp)
    check("prompt offers clinical_information", "clinical_information" in bp)
    # v4 (genomic_pathology_v4): Genomic_Variant_umbrella is re-introduced as a
    # separate section, so the shared profiler vocab now offers it again (it routes
    # variant rows to the revived genomic_variant_team). v3 runs simply never consume
    # that hint (no such team in teams.yaml), so this is additive + safe for v3.
    check("prompt offers Genomic_Variant_umbrella (v4 re-add)",
          "Genomic_Variant_umbrella" in bp)

    print("-" * 60)
    if fails:
        print(f"P3-M1 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("P3-M1 VERIFY: PASS — narrative block-roles added + v3 routing vocab.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
