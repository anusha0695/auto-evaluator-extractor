"""
V4-M2 gate — genomic_variant_team revived + bound to Genomic_Variant_umbrella (D1).

Locks (offline, deterministic):
  [1] config/teams_v4.yaml parses, points at the v4 schema, and registers
      genomic_variant_team → Genomic_Variant_umbrella with the HGNC + HGVS tools.
  [2] the v4 schema actually defines Genomic_Variant_umbrella (registry ↔ schema agree).
  [3] section toggle (D3): genomic_variant_team is enabled; specimen_findings_team +
      clinical_info_team are disabled; enabled_team_keys / disabled_sections agree.
  [4] config/prompts/genomic_variant_team.j2 is a REAL prompt again (not the tombstone):
      includes the base extractor prompt, targets Genomic_Variant_umbrella, and carries
      the new field cues (gene_studied, coding_dna_change, hgvs_validate).

Run:  PYTHONPATH=. python scripts/gates/gate_v4_m2_variant_team.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from agents.normalizer_hooks import build_normalizers
from core.section_toggle import disabled_sections, enabled_team_keys
from verification.normalization import NormalizationVerifier

_TEAMS = "config/teams_v4.yaml"
_SCHEMA = "config/schemas/genomic_pathology_v4.json"
_PROMPT = "config/prompts/genomic_variant_team.j2"
_NER = "config/ner_mapping.yaml"
_PROFILER = "config/prompts/preprocess/block_profiler.j2"
_NORMMAP = "config/normalizer_map.yaml"


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    print("[1] teams_v4.yaml registers genomic_variant_team")
    cfg = yaml.safe_load(Path(_TEAMS).read_text(encoding="utf-8"))
    check("schema_path points at v4", str(cfg.get("schema_path", "")).endswith("genomic_pathology_v4.json"),
          cfg.get("schema_path"))
    teams = cfg.get("teams") or {}
    gv = teams.get("genomic_variant_team") or {}
    check("genomic_variant_team present", bool(gv))
    check("bound to Genomic_Variant_umbrella", gv.get("schema_section") == "Genomic_Variant_umbrella",
          gv.get("schema_section"))
    tools = set(gv.get("tool_allowlist") or [])
    check("has hgnc_normalize + hgvs_validate", {"hgnc_normalize", "hgvs_validate"} <= tools, str(sorted(tools)))
    check("prompt_template wired", gv.get("prompt_template") == _PROMPT, gv.get("prompt_template"))

    print("[2] registry <-> schema agree on the section")
    schema = json.loads(Path(_SCHEMA).read_text(encoding="utf-8"))
    check("v4 schema defines Genomic_Variant_umbrella", "Genomic_Variant_umbrella" in schema["properties"])
    # every team's schema_section exists in the v4 schema
    bad = [k for k, t in teams.items() if t.get("schema_section") not in schema["properties"]]
    check("every team's schema_section exists in v4 schema", not bad, str(bad))

    print("[3] section toggle (D3)")
    ek = enabled_team_keys(cfg)
    check("genomic_variant_team enabled", "genomic_variant_team" in ek, str(ek))
    check("specimen_findings_team disabled", "specimen_findings_team" not in ek)
    check("clinical_info_team disabled", "clinical_info_team" not in ek)
    check("disabled_sections = significant_findings + clinical_information",
          disabled_sections(cfg) == {"significant_findings", "clinical_information"},
          str(disabled_sections(cfg)))

    print("[4] genomic_variant_team.j2 is a real prompt (not the tombstone)")
    p = Path(_PROMPT).read_text(encoding="utf-8")
    check("includes the base extractor prompt", "system/extractor.j2" in p)
    check("targets Genomic_Variant_umbrella", "Genomic_Variant_umbrella" in p)
    check("carries new variant field cues", all(s in p for s in ("gene_studied", "coding_dna_change", "hgvs_validate")))
    check("not the retired tombstone", "RETIRED" not in p.split("\n")[0] and "tombstone" not in p.lower())

    print("[5] M2b routing — NER + block-profiler vocab reach Genomic_Variant_umbrella")
    ner = yaml.safe_load(Path(_NER).read_text(encoding="utf-8"))
    l2u = ner.get("label_to_umbrellas") or {}
    check("GENE label routes to Genomic_Variant_umbrella",
          "Genomic_Variant_umbrella" in (l2u.get("GENE_OR_GENE_PRODUCT") or []))
    check("AMINO_ACID label routes to Genomic_Variant_umbrella",
          "Genomic_Variant_umbrella" in (l2u.get("AMINO_ACID") or []))
    prof = Path(_PROFILER).read_text(encoding="utf-8")
    check("block-profiler vocab includes Genomic_Variant_umbrella", "Genomic_Variant_umbrella" in prof)

    print("[6] HGNC verification revived (the dropped check) — gene-symbol canonicalization")
    norms = build_normalizers()
    check("hgnc normalizer adapter registered", "hgnc" in norms)
    nmap = (yaml.safe_load(Path(_NORMMAP).read_text(encoding="utf-8")) or {}).get("normalizable_fields") or {}
    check("normalizer_map maps gene_studied -> hgnc", nmap.get("gene_studied") == "hgnc", str(nmap.get("gene_studied")))
    # functional: NormalizationVerifier flags a non-canonical gene on a Genomic_Variant record
    nv = NormalizationVerifier()
    env = {"Genomic_Variant_umbrella": {"Genomic_Variants": [
        {"gene_studied": "JAK-2", "amino_acid_change": "V617F"}]}}
    res = nv.verify(envelope=env)
    flagged = {e.get("canonical") for e in res.get("field_errors") or []}
    check("verifier flags 'JAK-2' -> 'JAK2' on the variant gene", "JAK2" in flagged, str(res.get("field_errors")))

    print("[7] HGVS structural-validity floor (prefix-aware) — the malformed-HGVS check")
    from verification.hgvs_validity import find_malformed_hgvs, hgvs_field_valid
    check("well-formed c. is valid", hgvs_field_valid("coding_dna_change", "c.1799T>A"))
    check("bare 'V617F' valid after p. retry (legit verbatim, NOT flagged)",
          hgvs_field_valid("amino_acid_change", "V617F"))
    check("bare '1849G>T' valid after c. retry (legit verbatim)",
          hgvs_field_valid("coding_dna_change", "1849G>T"))
    check("garbled 'c.18_garbled!!' is invalid", not hgvs_field_valid("coding_dna_change", "c.18_garbled!!"))
    env_h = {"Genomic_Variant_umbrella": {"Genomic_Variants": [
        {"coding_dna_change": "c.1799T>A", "amino_acid_change": "V617F"},   # both fine
        {"coding_dna_change": "c.18_garbled!!"},                            # malformed
    ]}}
    mal = find_malformed_hgvs(env_h)
    refs = {m["ref"] for m in mal}
    check("only the garbled HGVS is flagged (verbatim/prefix-less NOT flagged)",
          refs == {"Genomic_Variant_umbrella.Genomic_Variants[1].coding_dna_change"}, str(mal))
    check("flagged status routes to review (invalid_hgvs, not renormalize)",
          all(m["status"] == "invalid_hgvs" for m in mal))

    print("-" * 60)
    if fails:
        print(f"V4-M2 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("V4-M2 VERIFY: PASS — variant team revived + bound + toggled + prompt; routing + HGNC + HGVS-validity.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
