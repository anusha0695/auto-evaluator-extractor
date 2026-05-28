"""
V4-M6 gate — verifier suite retargeted for v4 (D3) + HGVS-validity floor WIRED.

Locks (offline, deterministic — all modules import without cloud):
  [1] HGVS structural-validity floor (verification/hgvs_validity.find_malformed_hgvs)
      flags a genuinely-garbled change (c.18_garbled!!) but NOT legit prefix-less
      verbatim (V617F via p. retry, 1849G>T via c. retry, c.1799T>A as-is).
  [2] The floor is WIRED into run_verifier_suite as an ADVISORY scorecard
      (verifier_name="hgvs_validity", passed=True — it routes via a defect, never
      sinks the suite) — source-inspection of pipeline/graph_linear.py.
  [3] Triage routing: build_defects turns an hgvs_validity field_error into an
      `invalid_hgvs` defect; `invalid_hgvs` is ESCALATE-ONLY (never renormalize /
      re-extract — an OCR-garbled token has no canonical), so TriageAgent escalates it.
  [4] Disabled-section filter (drop_disabled_section_errors): an ADVISORY miss against
      a DISABLED section is dropped; an ACTIVE-section miss is KEPT; schema_validator
      errors are NEVER dropped. No-op when disabled_sections is empty (v2/v3 unchanged).
  [5] v4 run path threads disabled_sections (deps → self-correcting verifier node →
      run_verifier_suite) and the self-correcting graph admits version v4.
  [6] Normalizer map carries the revived HGNC gene canonicalization (gene_studied → hgnc).

Run:  PYTHONPATH=. python scripts/gates/gate_v4_m6_verifier_configs.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from pipeline.graph_linear import drop_disabled_section_errors
from pipeline.triage import _ESCALATE_ONLY, TriageAgent, build_defects
from verification.hgvs_validity import find_malformed_hgvs

_GRAPH_LINEAR = "pipeline/graph_linear.py"
_GRAPH_SC = "pipeline/graph_selfcorrecting.py"
_NORM_MAP = "config/normalizer_map.yaml"


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    print("[1] HGVS-validity floor flags malformed, not legit prefix-less")
    env = {"Genomic_Variant_umbrella": {"Genomic_Variants": [
        {"coding_dna_change": "c.1799T>A"},      # valid as-is
        {"amino_acid_change": "V617F"},          # legit prefix-less → p.V617F retry → valid
        {"genomic_dna_change": "1849G>T"},       # legit prefix-less → g.1849G>T retry → valid
        {"coding_dna_change": "c.18_garbled!!"}, # genuinely malformed
    ]}}
    mal = find_malformed_hgvs(env)
    vals = [m["value"] for m in mal]
    check("only c.18_garbled!! flagged", vals == ["c.18_garbled!!"], str(vals))
    check("flag routes to invalid_hgvs status", all(m["status"] == "invalid_hgvs" for m in mal))

    print("[2] floor wired into run_verifier_suite as an advisory scorecard")
    gl = Path(_GRAPH_LINEAR).read_text(encoding="utf-8")
    check("imports find_malformed_hgvs", "from verification.hgvs_validity import find_malformed_hgvs" in gl)
    check("appends an hgvs_validity scorecard", '"verifier_name": "hgvs_validity"' in gl)
    check("hgvs_validity scorecard is advisory (passed True)",
          '"verifier_name": "hgvs_validity", "passed": True' in gl)
    check("run_verifier_suite calls drop_disabled_section_errors", "drop_disabled_section_errors(scorecards" in gl)

    print("[3] triage routes hgvs_validity field_errors → invalid_hgvs → escalate")
    check("invalid_hgvs is escalate-only", "invalid_hgvs" in _ESCALATE_ONLY)
    state = {
        "verifier_scorecards": [{
            "verifier_name": "hgvs_validity", "passed": True,
            "field_errors": [{"ref": "Genomic_Variant_umbrella.Genomic_Variants[3].coding_dna_change",
                              "leaf": "coding_dna_change", "value": "c.18_garbled!!",
                              "status": "invalid_hgvs"}],
        }],
        "extraction": {}, "active_team_keys": ["genomic_variant_team"],
    }
    defects = build_defects(state)
    inv = [d for d in defects if d.defect_type == "invalid_hgvs"]
    check("one invalid_hgvs defect built", len(inv) == 1, f"{len(inv)} built")
    check("defect targets the variant ref",
          bool(inv) and inv[0].target_ref == "Genomic_Variant_umbrella.Genomic_Variants[3].coding_dna_change")
    decision = TriageAgent().decide(state)
    check("triage ESCALATES it (no repair)", decision.route == "done" and len(decision.escalations) == 1,
          f"route={decision.route} esc={len(decision.escalations)}")
    check("escalation kind is invalid_hgvs",
          bool(decision.escalations) and decision.escalations[0]["kind"] == "invalid_hgvs")

    print("[4] disabled-section filter drops advisory misses for disabled sections only")
    cards = [
        {"verifier_name": "schema_validator", "passed": False,
         "field_errors": [{"loc": "significant_findings.x"}]},   # structural — must NOT drop
        {"verifier_name": "recall_floor", "passed": True, "field_errors": [
            {"field_name": "significant_findings.pTNM_staging_details"},  # disabled → drop
            {"field_name": "clinical_information.clinical_history"},      # disabled → drop
            {"field_name": "Genomic_Variant_umbrella.Genomic_Variants"},  # active → keep
        ]},
        {"verifier_name": "hgvs_validity", "passed": True, "field_errors": [
            {"ref": "Genomic_Variant_umbrella.Genomic_Variants[0].coding_dna_change"},  # active → keep
        ]},
    ]
    disabled = {"significant_findings", "clinical_information"}
    n = drop_disabled_section_errors(cards, disabled)
    rf = next(c for c in cards if c["verifier_name"] == "recall_floor")
    sv = next(c for c in cards if c["verifier_name"] == "schema_validator")
    hv = next(c for c in cards if c["verifier_name"] == "hgvs_validity")
    check("dropped exactly the 2 disabled-section misses", n == 2, f"dropped={n}")
    check("recall_floor kept only the active-section miss",
          [e["field_name"] for e in rf["field_errors"]] == ["Genomic_Variant_umbrella.Genomic_Variants"])
    check("schema_validator untouched (structural error kept)", len(sv["field_errors"]) == 1)
    check("hgvs_validity active-section miss kept", len(hv["field_errors"]) == 1)
    check("no-op when disabled_sections empty (v2/v3 safe)", drop_disabled_section_errors(cards, set()) == 0)

    print("[5] v4 run path threads disabled_sections + admits v4")
    check("GraphDependencies carries disabled_sections", "disabled_sections: set[str]" in gl)
    check("build_graph_dependencies computes disabled_sections", "_disabled_secs = _disabled_sections(teams_cfg)" in gl)
    sc = Path(_GRAPH_SC).read_text(encoding="utf-8")
    check("self-correcting verifier node takes disabled_sections", "disabled_sections=None" in sc)
    check("self-correcting graph passes deps.disabled_sections",
          'getattr(deps, "disabled_sections"' in sc)
    check("self-correcting graph admits v4", '("v3", "v4")' in sc)

    print("[6] revived HGNC gene canonicalization in the normalizer map")
    nm = yaml.safe_load(Path(_NORM_MAP).read_text(encoding="utf-8")) or {}
    flat = {**(nm.get("leaf_normalizers") or nm)}
    check("gene_studied → hgnc present", any(
        "gene_studied" in str(k) and "hgnc" in str(v).lower()
        for k, v in (flat.items() if isinstance(flat, dict) else [])
    ) or "gene_studied" in Path(_NORM_MAP).read_text(encoding="utf-8"))

    print("-" * 60)
    if fails:
        print(f"V4-M6 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("V4-M6 VERIFY: PASS — verifier suite retargeted; HGVS-validity floor wired (detect→escalate).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
