"""
Phase 3 M10e gate — normalization floor → renormalize_field (gap #2). Offline.

Checks:
  [1] NormalizationVerifier flags fields whose deterministic canonical DIFFERS from
      the extracted value (biomarker_name / method / HGVS); leaves already-canonical
      or unmatched values alone. Builds its own offline normalizers.
  [2] build_defects turns the normalization scorecard into normalization_invalid
      defects; triage routes them to renormalize_field (team-less, not escalated).
  [3] RepairExecutor.renormalize writes the canonical ONLY when matched + different,
      preserves the verbatim occurrence, and no-ops on no-match.

Run:  PYTHONPATH=. python scripts/gates/gate_p3_m10e_normalization_floor.py
"""

from __future__ import annotations

import asyncio
import sys

from agents.normalizer_hooks import build_normalizers
from pipeline.repair import RepairExecutor
from pipeline.triage import TriageAgent, build_defects
from verification.normalization import NormalizationVerifier


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    nz = build_normalizers()
    # sanity: the adapters return {value, matched}
    print("[0] normalizer adapters")
    bm = nz["biomarker"]("her-2")
    check("biomarker adapter returns {value,matched}", "value" in bm and "matched" in bm, str(bm))

    print("[1] NormalizationVerifier flags canonical-differs fields")
    envelope = {
        "other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": [
            {"biomarker_name": "her-2", "findings": [{"result": "Detected", "method": "n.g.s."}]}]},
    }
    sc = NormalizationVerifier(normalizers=nz).verify(envelope=envelope)
    check("verifier_name == normalization", sc["verifier_name"] == "normalization")
    refs = {e["ref"]: e for e in sc["field_errors"]}
    bn = "other_molecular_biomarker_umbrella.other_molecular_biomarkers[0].biomarker_name"
    check("biomarker_name flagged (her-2 → HER2)", bn in refs and refs[bn]["canonical"] != "her-2", str(refs.get(bn)))
    check("flagged entry carries normalizer_key + canonical",
          bn in refs and refs[bn].get("normalizer_key") == "biomarker" and refs[bn].get("canonical"))
    # an already-canonical value is NOT flagged
    sc2 = NormalizationVerifier(normalizers=nz).verify(envelope={
        "other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": [{"biomarker_name": "HER2"}]}})
    check("already-canonical biomarker NOT flagged",
          not any("biomarker_name" in e["ref"] for e in sc2["field_errors"]), str(sc2["field_errors"]))

    print("[2] build_defects → normalization_invalid → renormalize_field")
    state = {"doc_id": "t", "active_team_keys": ["molecular_biomarker_team"],
             "verifier_scorecards": [sc], "binding_verifier": {}, "extraction": envelope}
    defs = build_defects(state)
    nd = [d for d in defs if d.defect_type == "normalization_invalid"]
    check("normalization_invalid defect produced", len(nd) >= 1, str([d.defect_type for d in defs]))
    check("detail carries key + input→canonical", nd and "biomarker:" in nd[0].detail, nd[0].detail if nd else "")
    dec = TriageAgent().decide(state)
    acts = {r["defect_type"]: r["action"] for r in dec.repair_requests}
    check("normalization_invalid → renormalize_field", acts.get("normalization_invalid") == "renormalize_field", str(acts))

    print("[3] RepairExecutor.renormalize writes canonical, preserves verbatim")
    ex = RepairExecutor(teams={}, normalizers=nz)
    so = {"other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": [
        {"biomarker_name": "her-2", "occurrences": [{"block_id": "b1", "surface": "her-2"}]}]}}
    st = {"section_outputs": so, "team_results": {}, "repair_log": [], "escalation_queue": [],
          "repair_budget_used": 0,
          "repair_requests": [{"action": "renormalize_field", "section": "other_molecular_biomarker_umbrella",
                               "target_ref": "other_molecular_biomarker_umbrella.other_molecular_biomarkers[0].biomarker_name",
                               "defect_type": "normalization_invalid", "detail": "biomarker: her-2 → HER2",
                               "signature": "n"}]}
    out = asyncio.run(ex.apply(st))
    rec = out["section_outputs"]["other_molecular_biomarker_umbrella"]["other_molecular_biomarkers"][0]
    check("field rewritten to canonical (HER2)", rec["biomarker_name"] == "HER2", rec["biomarker_name"])
    check("verbatim preserved in occurrences", rec["occurrences"] == [{"block_id": "b1", "surface": "her-2"}])
    check("repair_log outcome renormalized", out["repair_log"][0]["outcome"] == "renormalized")
    # no-op when no normalizer / no change
    ex2 = RepairExecutor(teams={}, normalizers={})
    st2 = {"section_outputs": so, "team_results": {}, "repair_log": [], "escalation_queue": [],
           "repair_budget_used": 0,
           "repair_requests": [{"action": "renormalize_field", "section": "x",
                                "target_ref": "x.y", "defect_type": "normalization_invalid",
                                "detail": "biomarker: a → b", "signature": "z"}]}
    out2 = asyncio.run(ex2.apply(st2))
    check("no normalizer injected → skipped (no crash)", out2["repair_log"][0]["outcome"] == "skipped_no_normalizer")

    print("-" * 60)
    if fails:
        print(f"P3-M10e VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("P3-M10e VERIFY: PASS — normalization floor → renormalize_field (canonical write, verbatim kept).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
