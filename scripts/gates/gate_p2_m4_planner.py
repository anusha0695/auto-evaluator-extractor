"""
Phase 2 M4 verification gate — deterministic Planner.

Done-when (PHASE_2_PLAN.md M4): on a demo-like signal set the planner activates
Metadata + GenomicVariant + TestedBiomarker and SKIPS MolecularBiomarker (no
TMB/MSI/PD-L1/HRD) and ClinicalInfo (no clinical-history signal).

Run:  PYTHONPATH=. python scripts/gates/gate_p2_m4_planner.py
"""

from __future__ import annotations

import sys

import yaml

from agents.planner import Planner


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    teams_cfg = yaml.safe_load(open("config/teams.yaml", encoding="utf-8"))
    team_keys = [
        "metadata_team",
        "molecular_biomarker_team", "tested_biomarker_team", "clinical_info_team",
    ]

    # Demo-like signal: a results table hinting biomarker + panel, and a parser
    # hypothesis candidate for the biomarker umbrella (v3 merge: a gene variant
    # is a biomarker). NO clinical-history signal.
    state = {
        "doc_id": "demo",
        "doc_profile": {
            "block_profiles": [
                {"block_id": "b1", "target_umbrella_hints": ["report_metadata"]},
                {"block_id": "b2", "target_umbrella_hints":
                    ["other_molecular_biomarker_umbrella", "tested_biomarker_umbrella"]},
                {"block_id": "b3", "target_umbrella_hints": ["none"]},
            ],
        },
        "parser_hypothesis": {
            "candidates": [
                {"text": "JAK2", "target_umbrella": "other_molecular_biomarker_umbrella"},
            ],
        },
    }

    plan = Planner().plan(state, teams_cfg=teams_cfg, team_keys=team_keys)
    print(f"  active={plan.active_team_keys}")
    print(f"  skipped={plan.skipped_team_keys}")

    active = set(plan.active_team_keys)
    skipped = set(plan.skipped_team_keys)
    check("metadata active (always-on)", "metadata_team" in active)
    check("molecular_biomarker active (gets the variant/biomarker signal)",
          "molecular_biomarker_team" in active)
    check("tested_biomarker active", "tested_biomarker_team" in active)
    check("clinical_info SKIPPED", "clinical_info_team" in skipped)
    check("rationale present for every team", set(plan.rationale) == set(team_keys))

    # Negative control: add a clinical-history signal → clinical_info activates.
    state["doc_profile"]["block_profiles"].append(
        {"block_id": "b4", "target_umbrella_hints": ["clinical_information"]})
    plan2 = Planner().plan(state, teams_cfg=teams_cfg, team_keys=team_keys)
    check("clinical_info activates once signalled",
          "clinical_info_team" in set(plan2.active_team_keys))

    print("-" * 60)
    if fails:
        print(f"M4 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("M4 VERIFY: PASS — planner activates/skips teams by signal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
