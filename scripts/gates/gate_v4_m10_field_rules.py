"""
V4-M10 gate — concatenation `" | "` + VERBATIM/DERIVED/null + count discipline.

The discipline is enforced CENTRALLY in config/prompts/system/extractor.j2, which every
team prompt `{% include %}`s — so a single source of truth governs all v4 teams. The two
v4 ARRAY prompts (variant, flat biomarker) additionally restate the per-section concat +
count rules.

Locks (offline, deterministic — text inspection):
  [1] the base prompt states all four rules: VERBATIM, DERIVED, the `" | "` (space-pipe-
      space) multi-fragment concatenation rule, null-not-fabricate, and the count_* rule.
  [2] every v4-ACTIVE team prompt includes the base (so it inherits the discipline):
      metadata, genomic_variant, molecular_biomarker_v4, tested.
  [3] the two v4 ARRAY prompts (genomic_variant, molecular_biomarker_v4) explicitly restate
      the `" | "` concat AND a count == number-of-records rule.
  [4] non-destructive: the v3 nested biomarker prompt (molecular_biomarker_team.j2) is
      untouched (still nested).

Run:  PYTHONPATH=. python scripts/gates/gate_v4_m10_field_rules.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_BASE = "config/prompts/system/extractor.j2"
_V4_ACTIVE = [
    "config/prompts/metadata_team.j2",
    "config/prompts/genomic_variant_team.j2",
    "config/prompts/molecular_biomarker_team_v4.j2",
    "config/prompts/tested_biomarker_team.j2",
]
_V4_ARRAY = [
    "config/prompts/genomic_variant_team.j2",
    "config/prompts/molecular_biomarker_team_v4.j2",
]
_V3_BIOMARKER = "config/prompts/molecular_biomarker_team.j2"


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    print("[1] base prompt centrally states the discipline")
    base = Path(_BASE).read_text(encoding="utf-8")
    check("states VERBATIM", "VERBATIM" in base)
    check("states DERIVED", "DERIVED" in base)
    check("states the \" | \" (space-pipe-space) concat rule",
          '" | "' in base and "space-pipe-space" in base)
    check("states null-not-fabricate", "null" in base and "Never fabricate" in base)
    check("states the count_* invariant", "count_*" in base or "count_" in base)

    print("[2] every v4-active team prompt includes the base")
    for p in _V4_ACTIVE:
        txt = Path(p).read_text(encoding="utf-8")
        check(f"{Path(p).name} includes system/extractor.j2", "system/extractor.j2" in txt)

    print("[3] v4 array prompts restate the concat + count rules")
    for p in _V4_ARRAY:
        txt = Path(p).read_text(encoding="utf-8")
        check(f"{Path(p).name} restates the \" | \" concat", "`" in txt and "| `" in txt or " | " in txt)
        check(f"{Path(p).name} restates a count == records rule",
              ("count_of_" in txt and any(kw in txt for kw in
               ("length", "number of", "= number", "records", "equal"))))

    print("[4] v3 nested biomarker prompt untouched (non-destructive)")
    v3 = Path(_V3_BIOMARKER).read_text(encoding="utf-8")
    check("v3 biomarker prompt still nested (findings)", "findings" in v3)

    print("-" * 60)
    if fails:
        print(f"V4-M10 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("V4-M10 VERIFY: PASS — concat/VERBATIM/DERIVED/null/count discipline consistent across v4 prompts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
