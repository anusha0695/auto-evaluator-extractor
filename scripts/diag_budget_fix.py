"""
Dummy-data verification of Fix 2 — deterministic repairs don't count against the
global budget. Mirrors the live case from the SME queue:

  Document has many addressable defects:
   - 1× re_extract_team (LLM-touching → counts toward budget)
   - 2× re_extract_team (LLM-touching → counts)
   - 3× re_extract_team (LLM-touching → counts)
   - 4× re_extract_team (LLM-touching → counts)
   - 5× re_extract_team (LLM-touching → counts)
   - 6× re_extract_team (LLM-touching → counts)
   - 7× re_extract_team (LLM-touching → counts)
   - 8× re_extract_team (LLM-touching → counts)  ← budget cap of 8 reached
   - 9× renormalize_field "Next-generation sequencing → NGS"  (deterministic)
   - 10× renormalize_field "PCR  →  PCR" deterministic too

  Before Fix 2: items 9 and 10 escalate ("global repair budget (8) exhausted").
  After Fix 2:  items 9 and 10 ride through under the deterministic exemption.
"""
from __future__ import annotations

import sys

from pipeline.triage import TriageAgent, build_defects


def main() -> int:
    # 8 normalization errors → triage maps each to re_extract_team via the path? No —
    # normalization_invalid maps to renormalize_field (per _ADDRESSABLE). To trigger
    # the budget pressure we need a MIX: schema_errors fill the budget with
    # re_extract_team requests, then normalization_invalid asks for renormalize_field.
    schema_field_errors = [
        {"loc": f"Genomic_Variant_umbrella.Genomic_Variants[{i}].field", "msg": "missing"}
        for i in range(8)                               # fills budget
    ]
    normalization_errors = [
        {"ref": "other_molecular_biomarker_umbrella.other_molecular_biomarkers[1].method",
         "normalizer_key": "method", "input": "Next-generation sequencing", "canonical": "NGS"},
        {"ref": "other_molecular_biomarker_umbrella.other_molecular_biomarkers[2].method",
         "normalizer_key": "method", "input": "Next-generation sequencing", "canonical": "NGS"},
    ]
    state = {
        "extraction": {},
        "active_team_keys": ["genomic_variant_team", "molecular_biomarker_team",
                             "tested_biomarker_team", "metadata_team"],
        "verifier_scorecards": [
            {"verifier_name": "schema_validator", "passed": False,
             "field_errors": schema_field_errors},
            {"verifier_name": "normalization", "passed": True,
             "field_errors": normalization_errors},
        ],
        "binding_verifier": {"refuted": 0, "uncertain": 0},
        "binding_items": [],
    }

    defects = build_defects(state)
    schema_count = sum(1 for d in defects if d.defect_type == "schema_error")
    norm_count = sum(1 for d in defects if d.defect_type == "normalization_invalid")
    print(f"defects built: schema_error={schema_count}  normalization_invalid={norm_count}")

    decision = TriageAgent().decide(state)
    re_extract = [r for r in decision.repair_requests if r["action"] == "re_extract_team"]
    renorm = [r for r in decision.repair_requests if r["action"] == "renormalize_field"]
    escalated_norm = [e for e in decision.escalations if e.get("kind") == "normalization_invalid"]

    print(f"repair requests: re_extract_team={len(re_extract)}  renormalize_field={len(renorm)}")
    print(f"escalations of kind normalization_invalid: {len(escalated_norm)}")

    # Active teams = 4 → global cap = 4 × 2 = 8. With 8 re_extract requests, budget is
    # fully used by LLM-touching actions. Before Fix 2: the 2 renormalize_field requests
    # would have escalated. After Fix 2: they ride through.
    expect_renorm = 2
    expect_esc = 0
    if len(renorm) == expect_renorm and len(escalated_norm) == expect_esc:
        print("RESULT: PASS — deterministic renormalize_field exempt from budget cap.")
        return 0
    print(f"RESULT: FAIL — expected renorm={expect_renorm}/{expect_esc}, "
          f"got renorm={len(renorm)}/{len(escalated_norm)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
