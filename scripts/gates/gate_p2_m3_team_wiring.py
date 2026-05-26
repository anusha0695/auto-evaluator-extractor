"""
Phase 2 M3 verification gate — specialist team wiring (NO LLM calls).

Done-when (deterministic portion of PHASE_2_PLAN.md M3):
  - build_section_team(...) constructs each 2a team (Extractor+CoverageAuditor+
    Arbiter) against schema v3;
  - each team's schema_section matches teams.yaml;
  - each team's tool_allowlist resolves to real registry tools (incl. the new
    M2 normalization tools);
  - each team's Extractor system prompt renders (StrictUndefined-clean);
  - the needs_review escalation counter (OCR/VMAW carrier) works.

The actual end-to-end LLM run on demo.pdf is the M3 CHECKPOINT (needs GCP/Gemini
creds) — run with `make run-local PDF=./data/actual_docs/demo.pdf` (graph_linear).

Run:  PYTHONPATH=. python scripts/gates/gate_p2_m3_team_wiring.py
"""

from __future__ import annotations

import sys

import yaml

from core.prompt_renderer import PromptRenderer, ToolDescriptor
from core.schema_loader import SchemaLoader
from core.tool_registry import build_tools_for_state, descriptors_from_yaml
from teams import SectionTeam, build_section_team

V3 = "config/schemas/genomic_pathology_v3.json"
TEAMS = [
    "molecular_biomarker_team",
    "tested_biomarker_team",
    "clinical_info_team",
]


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    sl = SchemaLoader.from_path(V3)
    pr = PromptRenderer(schema_loader=sl)
    teams_cfg = yaml.safe_load(open("config/teams.yaml", encoding="utf-8"))

    print("[1] build each 2a team (no LLM) + wiring")
    for key in TEAMS:
        cfg = teams_cfg["teams"][key]
        try:
            team = build_section_team(
                key, teams_cfg=teams_cfg, schema_loader=sl, prompt_renderer=pr,
            )
        except Exception as exc:  # noqa: BLE001
            check(f"{key}: build", False, str(exc)); continue
        check(f"{key}: is SectionTeam + section matches",
              isinstance(team, SectionTeam) and team.schema_section == cfg["schema_section"],
              team.schema_section)

        allow = list(cfg["tool_allowlist"])
        try:
            tools = build_tools_for_state(state={}, allowlist=allow, schema_loader=sl)
            check(f"{key}: tool_allowlist resolves ({len(allow)})",
                  {t.name for t in tools} == set(allow))
        except Exception as exc:  # noqa: BLE001
            check(f"{key}: tool_allowlist resolves", False, str(exc))

        try:
            descs = descriptors_from_yaml(allowlist=allow)
            txt = pr.render_team_extractor_prompt(
                team_name=key,
                team_prompt_template=cfg["prompt_template"],
                schema_section=team.schema_section,
                pipeline_version="v2",
                available_tools=[ToolDescriptor(d.name, d.description, d.input_schema) for d in descs],
                parser_hypothesis_count=5,
            )
            check(f"{key}: extractor prompt renders",
                  team.schema_section in txt and "Field contract" in txt, f"{len(txt)} chars")
        except Exception as exc:  # noqa: BLE001
            check(f"{key}: extractor prompt renders", False, str(exc))

    print("[2] needs_review escalation counter (OCR/VMAW carrier)")
    sample = {"other_molecular_biomarkers": [
        {"biomarker_name": "HER2", "needs_review": False, "findings": []},
        {"biomarker_name": "JAK2", "needs_review": True, "findings": [
            {"method": "PCR", "result": "Detected", "needs_review": False}]},
    ]}
    n = SectionTeam._count_needs_review(sample)
    check("counts 1 flagged record", n == 1, str(n))
    check("counts 0 on clean output", SectionTeam._count_needs_review({"x": [{"y": 1}]}) == 0)

    print("[3] all teams use distinct v3 sections")
    sections = {teams_cfg["teams"][k]["schema_section"] for k in TEAMS}
    check("3 distinct sections (v3 merge folded the variant team away)",
          len(sections) == 3, str(sorted(sections)))
    check("clinical_information is v3-only section present", "clinical_information" in sections)

    print("-" * 60)
    if fails:
        print(f"M3 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("M3 VERIFY: PASS — 4 teams wire up against v3; tools + prompts resolve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
