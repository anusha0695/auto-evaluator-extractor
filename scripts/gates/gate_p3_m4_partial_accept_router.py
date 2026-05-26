"""
Phase 3 M4 gate — partial-accept router (decision/decision_router.py::decide_v3).

Checks the additive partial-accept verdict without any LLM/cloud:
  [1] all-clean teams + clean verifiers          → auto_accept (every section accepted)
  [2] one flagged team + clean others            → partial_accept (accepted + flagged + item)
  [3] structural schema_validator failure        → sme_flag (whole envelope, no partial)
  [4] no acceptable section                      → sme_flag
  [5] record-level escalation aggregation:
        - nested needs_review:true records walked out with section + dotted ref
        - recall_floor LOUD miss escalated, quiet miss NOT
        - binding uncertain / refuted escalated
  [6] node factory writes verdict + verdict_reason + router_decision(to_dict)

decide_v2 must remain intact (back-compat spot check).

Run:  PYTHONPATH=. python scripts/gates/gate_p3_m4_partial_accept_router.py
"""

from __future__ import annotations

import asyncio
import sys

from decision.decision_router import (
    DecisionRouter,
    RouterDecisionV3,
    make_decision_router_v3_node,
)

# every team clean @ high confidence, no needs_review
_CLEAN_TEAMS = {
    "metadata_team": {"verdict": "auto_accept", "llm_confidence_score": 0.95, "needs_review_count": 0},
    "molecular_biomarker_team": {"verdict": "auto_accept", "llm_confidence_score": 0.92, "needs_review_count": 0},
    "tested_biomarker_team": {"verdict": "auto_accept", "llm_confidence_score": 0.91, "needs_review_count": 0},
    "clinical_info_team": {"verdict": "auto_accept", "llm_confidence_score": 0.93, "needs_review_count": 0},
    "specimen_findings_team": {"verdict": "auto_accept", "llm_confidence_score": 0.90, "needs_review_count": 0},
}


def _sc(name, passed=True, field_errors=None, **extra):
    return {"verifier_name": name, "passed": passed,
            "field_errors": field_errors or [], "notes": extra.get("notes", "")}


def _clean_scorecards():
    return [_sc("schema_validator"), _sc("coverage_audit"), _sc("link_consistency"),
            _sc("evidence_confidence"), _sc("recall_floor")]


def main() -> int:
    fails: list[str] = []

    def check(label, cond, detail=""):
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            fails.append(label)

    r = DecisionRouter()  # threshold 0.85

    print("[1] all-clean → auto_accept")
    d = r.decide_v3({"team_results": _CLEAN_TEAMS, "verifier_scorecards": _clean_scorecards(),
                     "binding_verifier": {"refuted": 0, "uncertain": 0}, "extraction": {}})
    check("verdict auto_accept", d.verdict == "auto_accept", d.verdict)
    check("all 5 sections accepted", len(d.accepted_sections) == 5, str(d.accepted_sections))
    check("no flagged sections / items", not d.flagged_sections and not d.escalation_items)

    print("[2] one flagged team + clean others → partial_accept")
    teams = dict(_CLEAN_TEAMS)
    teams["clinical_info_team"] = {"verdict": "sme_flag", "llm_confidence_score": 0.4, "needs_review_count": 0}
    d = r.decide_v3({"team_results": teams, "verifier_scorecards": _clean_scorecards(),
                     "binding_verifier": {}, "extraction": {}})
    check("verdict partial_accept", d.verdict == "partial_accept", d.verdict)
    check("clinical_information NOT accepted", "clinical_information" not in d.accepted_sections)
    check("4 sections accepted", len(d.accepted_sections) == 4, str(d.accepted_sections))
    check("clinical_information flagged with reasons",
          any(f["section"] == "clinical_information" and f["reasons"] for f in d.flagged_sections),
          str(d.flagged_sections))

    print("[3] structural schema failure → sme_flag (whole envelope)")
    d = r.decide_v3({"team_results": _CLEAN_TEAMS,
                     "verifier_scorecards": [_sc("schema_validator", passed=False, notes="missing required field")]
                     + _clean_scorecards()[1:],
                     "binding_verifier": {}, "extraction": {}})
    check("verdict sme_flag", d.verdict == "sme_flag", d.verdict)
    check("no sections accepted on structural failure", d.accepted_sections == [])

    print("[4] no acceptable section → sme_flag")
    all_flagged = {k: {"verdict": "sme_flag", "llm_confidence_score": 0.2, "needs_review_count": 1}
                   for k in _CLEAN_TEAMS}
    d = r.decide_v3({"team_results": all_flagged, "verifier_scorecards": _clean_scorecards(),
                     "binding_verifier": {}, "extraction": {}})
    check("verdict sme_flag (nothing acceptable)", d.verdict == "sme_flag", d.verdict)
    check("no sections accepted", d.accepted_sections == [], str(d.accepted_sections))
    check("all 5 sections flagged with reasons",
          len(d.flagged_sections) == 5 and all(f["reasons"] for f in d.flagged_sections),
          str(d.flagged_sections))

    print("[5] record-level escalation aggregation")
    # (a) nested needs_review:true → walked out with section + dotted ref
    envelope = {"significant_findings": {"specimen_findings": [
        {"specimen_id": "A", "pTNM_staging_details": {"needs_review": True, "review_reason": "OCR low conf"}}]}}
    items = DecisionRouter._needs_review_items(envelope)
    nr = [i for i in items if i["kind"] == "needs_review"]
    check("needs_review record found", len(nr) == 1, str(nr))
    check("tagged with section significant_findings", nr and nr[0]["section"] == "significant_findings")
    check("dotted ref points into the record",
          nr and "specimen_findings[0].pTNM_staging_details" in nr[0]["ref"], nr[0]["ref"] if nr else "")
    # (b) recall_floor LOUD escalated, quiet NOT
    rf = _sc("recall_floor", field_errors=[
        {"field_name": "significant_findings.pTNM_stage", "role": "synoptic_report",
         "strictness": "loud", "status": "confirmed_miss"},
        {"field_name": "clinical_information.history", "role": "clinical_history",
         "strictness": "quiet", "status": "unconfirmed"}])
    d = r.decide_v3({"team_results": _CLEAN_TEAMS,
                     "verifier_scorecards": [_sc("schema_validator"), rf],
                     "binding_verifier": {}, "extraction": {}})
    kinds = [i["kind"] for i in d.escalation_items]
    check("recall_floor LOUD escalated", "recall_floor_loud" in kinds, str(kinds))
    check("recall_floor quiet NOT escalated",
          not any(i.get("ref") == "clinical_information.history" for i in d.escalation_items))
    check("LOUD miss flips verdict off auto_accept", d.verdict == "partial_accept", d.verdict)
    # (c) binding uncertain + refuted escalated
    d = r.decide_v3({"team_results": _CLEAN_TEAMS, "verifier_scorecards": _clean_scorecards(),
                     "binding_verifier": {"uncertain": 2, "refuted": 1}, "extraction": {}})
    kinds = {i["kind"] for i in d.escalation_items}
    check("binding_uncertain escalated", "binding_uncertain" in kinds, str(kinds))
    check("binding_refuted escalated", "binding_refuted" in kinds, str(kinds))

    print("[6] node factory writes verdict + router_decision")
    node = make_decision_router_v3_node(router=r)
    out = asyncio.run(node({"doc_id": "t", "team_results": _CLEAN_TEAMS,
                            "verifier_scorecards": _clean_scorecards(),
                            "binding_verifier": {}, "extraction": {}}))
    check("node returns verdict", out.get("verdict") == "auto_accept", str(out.get("verdict")))
    check("node returns verdict_reason str", isinstance(out.get("verdict_reason"), str))
    check("node returns router_decision dict with the partial-accept keys",
          isinstance(out.get("router_decision"), dict)
          and {"verdict", "reason", "accepted_sections", "flagged_sections", "escalation_items"}
          <= set(out["router_decision"]), str(out.get("router_decision")))

    print("[7] decide_v2 still intact (back-compat)")
    v2, _ = r.decide_v2({"team_results": _CLEAN_TEAMS, "verifier_scorecards": _clean_scorecards(),
                         "binding_verifier": {"refuted": 0, "uncertain": 0}})
    check("decide_v2 auto_accept on clean state", v2 == "auto_accept", v2)

    print("-" * 60)
    if fails:
        print(f"P3-M4 VERIFY: FAIL ({len(fails)}): {fails}")
        return 1
    print("P3-M4 VERIFY: PASS — partial-accept router commits clean sections, escalates the rest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
