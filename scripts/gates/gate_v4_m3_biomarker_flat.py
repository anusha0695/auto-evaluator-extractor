"""
V4-M3 gate — other_molecular_biomarker_umbrella flattened (D2).

Locks (offline, deterministic):
  [1] teams_v4 points molecular_biomarker_team at the v4 (flat) prompt; the v3 prompt
      (molecular_biomarker_team.j2) is UNTOUCHED (non-destructive, D5).
  [2] the v4 prompt is FLAT: documents the 6 prod fields, and does NOT carry the v3
      nested-model vocabulary (findings / occurrences / variant_detail / biomarker_class),
      and explicitly routes sequence variants AWAY (to Genomic_Variant_umbrella).
  [3] the v4 schema's other_molecular_biomarkers item is flat (cross-check).

Run:  PYTHONPATH=. python scripts/gates/gate_v4_m3_biomarker_flat.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

_TEAMS = "config/teams_v4.yaml"
_V4_PROMPT = "config/prompts/molecular_biomarker_team_v4.j2"
_V3_PROMPT = "config/prompts/molecular_biomarker_team.j2"
_SCHEMA = "config/schemas/genomic_pathology_v4.json"


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    print("[1] teams_v4 points the biomarker team at the v4 flat prompt")
    cfg = yaml.safe_load(Path(_TEAMS).read_text(encoding="utf-8"))
    mb = (cfg.get("teams") or {}).get("molecular_biomarker_team") or {}
    check("prompt_template = molecular_biomarker_team_v4.j2", mb.get("prompt_template") == _V4_PROMPT,
          mb.get("prompt_template"))
    # v3 prompt still the nested one (untouched) — non-destructive
    v3 = Path(_V3_PROMPT).read_text(encoding="utf-8")
    check("v3 biomarker prompt still nested (findings)", "findings" in v3)

    print("[2] the v4 prompt is FLAT")
    p = Path(_V4_PROMPT).read_text(encoding="utf-8")
    check("includes the base extractor prompt", "system/extractor.j2" in p)
    check("documents the 6 flat fields",
          all(f in p for f in ("biomarker_name", "method", "result", "reference_range",
                               "interpretation", "page_number")))
    # the v4 prompt should NOT instruct the nested v3 model
    for banned in ("findings[]", "occurrences[]", "variant_detail", "biomarker_class"):
        check(f"v4 prompt drops the v3 nested concept: {banned}", banned not in p, banned)
    check("v4 prompt routes sequence variants away (to Genomic_Variant_umbrella)",
          "Genomic_Variant_umbrella" in p)

    print("[3] v4 schema biomarker item is flat (cross-check)")
    schema = json.loads(Path(_SCHEMA).read_text(encoding="utf-8"))
    item = schema["properties"]["other_molecular_biomarker_umbrella"]["properties"][
        "other_molecular_biomarkers"]["items"]["properties"]
    check("schema item has no findings/variant_detail", "findings" not in item and "variant_detail" not in item)
    check("schema item has the 6 prod fields",
          {"page_number", "biomarker_name", "method", "result", "reference_range", "interpretation"} <= set(item))

    print("-" * 60)
    if fails:
        print(f"V4-M3 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("V4-M3 VERIFY: PASS — other_molecular_biomarker flattened (v4 prompt); v3 nested prompt intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
