"""
Phase 2b verification gate — SpecimenFindingsTeam (no LLM).

Done-when (deterministic portion of M12-M14):
  - specimen_findings_team builds via build_section_team against v3; prompt renders;
  - Planner ACTIVATES significant_findings when signalled, SKIPS it otherwise
    (molecular-only docs like the JAK2 demo);
  - the scorer scores significant_findings: matched specimens → F1 1.0 + field
    accuracy; a dropped specimen → recall 0.5; a wrong staging_system_version is
    caught (it IS scored).

The end-to-end LLM run needs a real multi-specimen surgical-pathology PDF.

Run:  PYTHONPATH=. python scripts/gates/gate_p2_2b_specimen_findings_team.py
"""

from __future__ import annotations

import copy
import json
import sys

import yaml

from agents.planner import Planner
from core.prompt_renderer import PromptRenderer, ToolDescriptor
from core.schema_loader import SchemaLoader
from scripts.score_against_ground_truth import score_all_sections
from teams import SectionTeam, build_section_team


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    sl = SchemaLoader.from_path("config/schemas/genomic_pathology_v3.json")
    pr = PromptRenderer(schema_loader=sl)
    teams_cfg = yaml.safe_load(open("config/teams.yaml", encoding="utf-8"))

    print("[1] team builds + prompt renders")
    team = build_section_team("specimen_findings_team", teams_cfg=teams_cfg, schema_loader=sl, prompt_renderer=pr)
    check("is SectionTeam for significant_findings",
          isinstance(team, SectionTeam) and team.schema_section == "significant_findings")
    txt = pr.render_team_extractor_prompt(
        team_name="SpecimenFindingsTeam", team_prompt_template="specimen_findings_team.j2",
        schema_section="significant_findings", pipeline_version="v2",
        available_tools=[ToolDescriptor("pdf_text_search", "x", {})], parser_hypothesis_count=0)
    check("prompt renders with staging-version rule",
          "significant_findings" in txt and "staging_system_version" in txt and "Final Diagnosis" in txt)

    print("[2] Planner gates significant_findings by signal")
    keys = list(teams_cfg["teams"].keys())
    state_no = {"doc_id": "demo", "doc_profile": {"block_profiles": [
        {"block_id": "b", "target_umbrella_hints": ["Genomic_Variant_umbrella"]}]},
        "parser_hypothesis": {"candidates": []}}
    plan_no = Planner().plan(state_no, teams_cfg=teams_cfg, team_keys=keys)
    check("skipped when no signal (molecular-only)", "specimen_findings_team" in plan_no.skipped_team_keys)
    state_yes = {"doc_id": "path", "doc_profile": {"block_profiles": [
        {"block_id": "b", "target_umbrella_hints": ["significant_findings"]}]},
        "parser_hypothesis": {"candidates": []}}
    plan_yes = Planner().plan(state_yes, teams_cfg=teams_cfg, team_keys=keys)
    check("activated when signalled", "specimen_findings_team" in plan_yes.active_team_keys)

    print("[3] scorer scores significant_findings")
    gt_env = json.load(open("ground_truth/specimen_demo.json", encoding="utf-8"))["genomic_pathology_extraction"]
    # perfect extraction → F1 1.0, field accuracy 1.0
    ex = copy.deepcopy(gt_env)
    sf = {s.section: s for s in score_all_sections("specimen_demo", gt_env, ex)}["significant_findings"]
    check("2 specimens matched, F1=1.0", sf.f1 == 1.0 and sf.matched == 2, f"matched={sf.matched} f1={sf.f1}")
    check("field accuracy 1.0 on perfect match", sf.field_pass_rate == 1.0, str(sf.field_pass_rate))

    # drop specimen B → recall 0.5
    ex2 = copy.deepcopy(gt_env)
    ex2["significant_findings"]["specimen_findings"] = ex2["significant_findings"]["specimen_findings"][:1]
    sf2 = {s.section: s for s in score_all_sections("x", gt_env, ex2)}["significant_findings"]
    check("dropped specimen → recall 0.5", abs(sf2.recall - 0.5) < 1e-6, f"R={sf2.recall}")

    # wrong staging_system_version → field accuracy drops (it IS scored)
    ex3 = copy.deepcopy(gt_env)
    ex3["significant_findings"]["specimen_findings"][0]["pTNM_staging_details"]["staging_system_version"] = "AJCC 7th Edition"
    sf3 = {s.section: s for s in score_all_sections("x", gt_env, ex3)}["significant_findings"]
    check("wrong staging version lowers field accuracy", sf3.field_pass_rate < 1.0, str(sf3.field_pass_rate))

    print("-" * 60)
    if fails:
        print(f"2b VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("2b VERIFY: PASS — SpecimenFindingsTeam wired + planner-gated + scored.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
