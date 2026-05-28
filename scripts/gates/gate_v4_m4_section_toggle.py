"""
V4-M4 gate — honor the declarative `enabled` toggle on the v4 run path (D3).

Locks (offline, deterministic):
  [1] core.section_toggle semantics: absent/True/None → enabled; only literal False
      (or 'false'/'no'/'off'/'0') disables. filter_enabled_keys is subtractive +
      order-preserving + a safe no-op when nothing is disabled (v3-safe).
  [2] teams_v4.yaml: specimen_findings_team + clinical_info_team carry `enabled: false`;
      the other four teams have no `enabled` key (so they stay on).
  [3] enabled_team_keys(teams_v4) = the 4 active teams (metadata, genomic_variant,
      molecular_biomarker, tested_biomarker) and EXCLUDES the 2 disabled ones.
      disabled_sections(teams_v4) = {significant_findings, clinical_information}.
  [4] graph_linear.build_graph_dependencies applies filter_enabled_keys (so a disabled
      team never becomes a SectionTeam) — proven by source-inspection, no cloud build.
  [5] runner.py + process_local.py admit version "v4"; v3 teams.yaml stays toggle-free.

Run:  PYTHONPATH=. python scripts/gates/gate_v4_m4_section_toggle.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from core.section_toggle import (
    disabled_sections,
    enabled_team_keys,
    filter_enabled_keys,
    is_team_enabled,
)

_TEAMS_V4 = "config/teams_v4.yaml"
_TEAMS_V3 = "config/teams.yaml"
_GRAPH_LINEAR = "pipeline/graph_linear.py"
_RUNNER = "pipeline/runner.py"
_PROCESS_LOCAL = "scripts/process_local.py"

_ENABLED_EXPECTED = [
    "metadata_team",
    "genomic_variant_team",
    "molecular_biomarker_team",
    "tested_biomarker_team",
]
_DISABLED_EXPECTED = {"specimen_findings_team", "clinical_info_team"}
_DISABLED_SECTIONS_EXPECTED = {"significant_findings", "clinical_information"}


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    print("[1] section_toggle semantics")
    check("absent enabled key → enabled", is_team_enabled({"schema_section": "x"}))
    check("None config → enabled", is_team_enabled(None))
    check("enabled: true → enabled", is_team_enabled({"enabled": True}))
    check("enabled: false → disabled", not is_team_enabled({"enabled": False}))
    check("enabled: 'off' (str) → disabled", not is_team_enabled({"enabled": "off"}))
    check("enabled: 'no' (str) → disabled", not is_team_enabled({"enabled": "no"}))
    # filter_enabled_keys is subtractive + order-preserving + v3-safe no-op
    _cfg = {"teams": {"a": {}, "b": {"enabled": False}, "c": {"enabled": True}}}
    check("filter drops disabled, preserves order",
          filter_enabled_keys(["a", "b", "c"], _cfg) == ["a", "c"],
          str(filter_enabled_keys(["a", "b", "c"], _cfg)))
    check("filter is a no-op when nothing disabled (v3-safe)",
          filter_enabled_keys(["a", "c"], {"teams": {"a": {}, "c": {}}}) == ["a", "c"])

    print("[2] teams_v4.yaml carries the toggle on the right two teams")
    v4 = yaml.safe_load(Path(_TEAMS_V4).read_text(encoding="utf-8"))
    teams = v4["teams"]
    check("specimen_findings_team enabled: false", teams["specimen_findings_team"].get("enabled") is False)
    check("clinical_info_team enabled: false", teams["clinical_info_team"].get("enabled") is False)
    for k in _ENABLED_EXPECTED:
        check(f"{k} has no enabled key (stays on)", "enabled" not in teams[k])

    print("[3] enabled_team_keys / disabled_sections compute the v4 active set")
    got = enabled_team_keys(v4)
    check("enabled_team_keys == the 4 active teams (in registry order)", got == _ENABLED_EXPECTED, str(got))
    for k in _DISABLED_EXPECTED:
        check(f"{k} excluded from enabled set", k not in got)
    dsec = disabled_sections(v4)
    check("disabled_sections == {significant_findings, clinical_information}",
          dsec == _DISABLED_SECTIONS_EXPECTED, str(dsec))

    print("[4] graph_linear applies filter_enabled_keys (source-inspection)")
    gl = Path(_GRAPH_LINEAR).read_text(encoding="utf-8")
    check("imports filter_enabled_keys", "filter_enabled_keys" in gl)
    check("filters keys before building teams",
          gl.index("filter_enabled_keys(keys") < gl.index("build_section_team("))

    print("[5] v4 admitted on the run path; v3 stays toggle-free")
    rn = Path(_RUNNER).read_text(encoding="utf-8")
    check("runner admits version v4", '"v4"' in rn and "genomic_pathology_v4.json" in rn)
    check("runner v4 uses enabled_team_keys", "enabled_team_keys" in rn)
    pl = Path(_PROCESS_LOCAL).read_text(encoding="utf-8")
    check("process_local --version offers v4", '"v4"' in pl)
    v3 = yaml.safe_load(Path(_TEAMS_V3).read_text(encoding="utf-8"))
    check("v3 teams.yaml has no enabled toggles (unchanged)",
          all("enabled" not in (c or {}) for c in (v3.get("teams") or {}).values()))

    print("-" * 60)
    if fails:
        print(f"V4-M4 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("V4-M4 VERIFY: PASS — section toggle honored on the v4 run path; v3 untouched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
