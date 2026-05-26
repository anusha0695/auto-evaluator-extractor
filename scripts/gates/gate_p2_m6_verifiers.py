"""
Phase 2 M6 verification gate — verifier suite (deterministic parts; no LLM).

Done-when (deterministic portion of PHASE_2_PLAN.md M6):
  - CoverageVerifier flags a located entity absent from output;
  - LinkConsistencyVerifier flags a dangling link ref;
  - EvidenceConfidenceVerifier flags a populated record lacking occurrences[];
  - LinkBindingVerifier V1 confirms a grounded finding and refutes an ungrounded
    one; V3 refutes a hallucinated biomarker; a table-row bind skips V2; a
    narrative bind returns V2 uncertain in deterministic-only mode (escalate).

The planted EGFR/HER2 mis-bind (V2 LLM) is exercised at the M8 checkpoint.

Run:  PYTHONPATH=. python scripts/gates/gate_p2_m6_verifiers.py
"""

from __future__ import annotations

import sys

from agents.link_binding_verifier import LinkBindingVerifier
from agents.linker import Link
from verification.core_verifiers import (
    CoverageVerifier,
    EvidenceConfidenceVerifier,
    LinkConsistencyVerifier,
)


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    print("[1] CoverageVerifier")
    env = {"tested_biomarker_umbrella": {"tested_biomarkers": ["JAK2", "BRAF"]}}
    ph_ok = {"candidates": [{"text": "JAK2", "target_umbrella": "tested_biomarker_umbrella"}]}
    ph_miss = {"candidates": [{"text": "EGFR", "target_umbrella": "tested_biomarker_umbrella"}]}
    check("covered candidate passes", CoverageVerifier().verify(env, ph_ok)["passed"])
    # default is ADVISORY (passed=True); hard_gate=True restores gating.
    miss = CoverageVerifier(gap_tolerance=0.0, hard_gate=True).verify(env, ph_miss)
    check("missing candidate flagged (hard_gate)", not miss["passed"] and len(miss["field_errors"]) == 1)
    check("advisory mode never hard-fails", CoverageVerifier(gap_tolerance=0.0).verify(env, ph_miss)["passed"])

    print("[2] LinkConsistencyVerifier")
    # v3 merge: variants are biomarker entries; refs point into the biomarker umbrella.
    env2 = {
        "other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": [
            {"biomarker_name": "JAK2", "findings": [{"result": "Detected"}]}]},
        "tested_biomarker_umbrella": {"tested_biomarkers": ["JAK2"]},
    }
    good = [Link("other_molecular_biomarker_umbrella.other_molecular_biomarkers[0]",
                 "tested_biomarker_umbrella.tested_biomarkers[0]", "variant_on_panel", "r")]
    bad = [Link("other_molecular_biomarker_umbrella.other_molecular_biomarkers[5]",
                "tested_biomarker_umbrella.tested_biomarkers[0]", "variant_on_panel", "r")]
    check("valid links pass", LinkConsistencyVerifier().verify(env2, good)["passed"])
    check("dangling ref flagged", not LinkConsistencyVerifier().verify(env2, bad)["passed"])

    print("[3] EvidenceConfidenceVerifier")
    # A sequence-variant finding (variant_detail) with no occurrences must be flagged.
    grounded_env = {"other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": [
        {"biomarker_name": "JAK2", "findings": [
            {"result": "Detected", "occurrences": [{"block_id": "b1", "surface": "JAK2"}],
             "variant_detail": {"amino_acid_change": "p.V617F"}}]}]}}
    ungrounded_env = {"other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": [
        {"biomarker_name": "JAK2", "findings": [
            {"result": "Detected", "occurrences": [],
             "variant_detail": {"amino_acid_change": "p.V617F"}}]}]}}
    check("grounded variant finding passes", EvidenceConfidenceVerifier().verify(grounded_env)["passed"])
    check("ungrounded variant finding flagged", not EvidenceConfidenceVerifier().verify(ungrounded_env)["passed"])

    print("[4] LinkBindingVerifier (V1 grounding, V3 hallucination, V2 skip/uncertain)")
    blocks = [
        {"block_id": "tb1", "text": "HER2 IHC 2+", "table_id": "T1"},   # table-row bind
        {"block_id": "n1", "text": "PD-L1 expression is high by IHC."},  # narrative
    ]
    env3 = {"other_molecular_biomarker_umbrella": {"other_molecular_biomarkers": [
        {"biomarker_name": "HER2", "findings": [
            {"method": "IHC", "result": "2+", "occurrences": [{"block_id": "tb1", "surface": "HER2 IHC 2+"}]}]},
        {"biomarker_name": "PD-L1", "findings": [
            {"method": "IHC", "result": "high", "occurrences": [{"block_id": "n1", "surface": "PD-L1 ... high"}]}]},
        {"biomarker_name": "ZZZX", "findings": [   # hallucination — not in any block
            {"method": "IHC", "result": "pos", "occurrences": [{"block_id": "n1", "surface": "x"}]}]},
    ]}}
    res = LinkBindingVerifier().verify(envelope=env3, links=[], blocks=blocks)
    v = {(x.ref, x.check): x.verdict for x in res.verdicts}
    check("HER2 table-row V1 confirmed",
          v.get(("other_molecular_biomarkers[0].findings[0]", "V1")) == "confirmed")
    check("HER2 table-row SKIPS V2",
          ("other_molecular_biomarkers[0].findings[0]", "V2") not in v)
    check("PD-L1 narrative V2 uncertain (escalate)",
          v.get(("other_molecular_biomarkers[1].findings[0]", "V2")) == "uncertain")
    check("ZZZX hallucination V3 refuted",
          v.get(("other_molecular_biomarkers[2]", "V3")) == "refuted",
          str([x for x in res.verdicts if x.check == "V3"]))

    print("-" * 60)
    if fails:
        print(f"M6 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("M6 VERIFY: PASS — deterministic verifiers + V1/V3 + V2 escalation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
